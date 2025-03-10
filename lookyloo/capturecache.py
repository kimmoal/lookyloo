#!/usr/bin/env python3

import json
import logging
import pickle
import sys
import time

from collections.abc import Mapping
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import dns.rdatatype
import dns.resolver
from har2tree import CrawledTree, Har2TreeError, HarFile
from redis import Redis

from .context import Context
from .indexing import Indexing
from .default import LookylooException, try_make_file, get_config
from .exceptions import MissingCaptureDirectory, NoValidHarFile, MissingUUID, TreeNeedsRebuild


class CaptureCache():
    __slots__ = ('uuid', 'title', 'timestamp', 'url', 'redirects', 'capture_dir',
                 'error', 'incomplete_redirects', 'no_index', 'categories', 'parent')

    def __init__(self, cache_entry: Dict[str, Any]):
        __default_cache_keys: Tuple[str, str, str, str, str, str] = ('uuid', 'title', 'timestamp', 'url', 'redirects', 'capture_dir')
        if all(key in cache_entry.keys() for key in __default_cache_keys):
            self.uuid: str = cache_entry['uuid']
            self.title: str = cache_entry['title']
            try:
                self.timestamp: datetime = datetime.strptime(cache_entry['timestamp'], '%Y-%m-%dT%H:%M:%S.%f%z')
            except ValueError:
                # If the microsecond is missing (0), it fails
                self.timestamp = datetime.strptime(cache_entry['timestamp'], '%Y-%m-%dT%H:%M:%S%z')
            self.url: str = cache_entry['url']
            self.redirects: List[str] = json.loads(cache_entry['redirects'])
            self.capture_dir: Path = Path(cache_entry['capture_dir'])
            if not self.capture_dir.exists():
                raise MissingCaptureDirectory(f'The capture {self.uuid} does not exists in {self.capture_dir}.')
        elif not cache_entry.get('error'):
            missing = set(__default_cache_keys) - set(cache_entry.keys())
            raise LookylooException(f'Missing keys ({missing}), no error message. It should not happen.')

        # Error without all the keys in __default_cache_keys was fatal.
        # if the keys in __default_cache_keys are present, it was an HTTP error
        self.error: Optional[str] = cache_entry.get('error')
        self.incomplete_redirects: bool = True if cache_entry.get('incomplete_redirects') in [1, '1'] else False
        self.no_index: bool = True if cache_entry.get('no_index') in [1, '1'] else False
        self.categories: List[str] = json.loads(cache_entry['categories']) if cache_entry.get('categories') else []
        self.parent: Optional[str] = cache_entry.get('parent')

    @property
    def tree(self) -> CrawledTree:
        return load_pickle_tree(self.capture_dir, self.capture_dir.stat().st_mtime)


def remove_pickle_tree(capture_dir: Path) -> None:
    pickle_file = capture_dir / 'tree.pickle'
    if pickle_file.exists():
        pickle_file.unlink()


@lru_cache(maxsize=256)
def load_pickle_tree(capture_dir: Path, last_mod_time: int) -> CrawledTree:
    pickle_file = capture_dir / 'tree.pickle'
    if pickle_file.exists():
        with pickle_file.open('rb') as _p:
            try:
                tree = pickle.load(_p)
                if tree.root_hartree.har.path.exists():
                    return tree
                else:
                    # The capture was moved.
                    remove_pickle_tree(capture_dir)
            except pickle.UnpicklingError:
                remove_pickle_tree(capture_dir)
            except EOFError:
                remove_pickle_tree(capture_dir)
            except Exception:
                remove_pickle_tree(capture_dir)
    raise TreeNeedsRebuild()


class CapturesIndex(Mapping):

    def __init__(self, redis: Redis, contextualizer: Optional[Context]=None):
        self.logger = logging.getLogger(f'{self.__class__.__name__}')
        self.logger.setLevel(get_config('generic', 'loglevel'))
        self.redis = redis
        self.indexing = Indexing()
        self.contextualizer = contextualizer
        self.__cache: Dict[str, CaptureCache] = {}
        self._quick_init()

    def __getitem__(self, uuid: str) -> CaptureCache:
        if uuid in self.__cache:
            if (self.__cache[uuid].capture_dir.exists()
                    and not self.__cache[uuid].incomplete_redirects):
                return self.__cache[uuid]
            del self.__cache[uuid]
        capture_dir = self._get_capture_dir(uuid)
        cached = self.redis.hgetall(str(capture_dir))
        if cached:
            cc = CaptureCache(cached)
            # NOTE: checking for pickle to exist may be a bad idea here.
            if (cc.capture_dir.exists()
                    and (cc.capture_dir / 'tree.pickle').exists()
                    and not cc.incomplete_redirects):
                self.__cache[uuid] = cc
                return self.__cache[uuid]
        try:
            tree = load_pickle_tree(capture_dir, capture_dir.stat().st_mtime)
        except TreeNeedsRebuild:
            tree = self._create_pickle(capture_dir)
            self.indexing.new_internal_uuids(tree)
        self.__cache[uuid] = self._set_capture_cache(capture_dir, tree)
        return self.__cache[uuid]

    def __iter__(self):
        return iter(self.__cache)

    def __len__(self):
        return len(self.__cache)

    def reload_cache(self, uuid: str) -> None:
        if uuid in self.__cache:
            del self.__cache[uuid]

    def remove_pickle(self, uuid: str) -> None:
        if uuid in self.__cache:
            remove_pickle_tree(self.__cache[uuid].capture_dir)
            del self.__cache[uuid]

    def rebuild_all(self) -> None:
        for uuid, cache in self.__cache.items():
            remove_pickle_tree(cache.capture_dir)
        self.redis.flushdb()
        self.__cache = {}

    def lru_cache_status(self):
        return load_pickle_tree.cache_info()

    def _quick_init(self) -> None:
        '''Initialize the cache with a list of UUIDs, with less back and forth with redis.
        Only get recent captures.'''
        p = self.redis.pipeline()
        for directory in self.redis.hvals('lookup_dirs'):
            p.hgetall(directory)
        for cache in p.execute():
            if not cache:
                continue
            try:
                cc = CaptureCache(cache)
            except LookylooException as e:
                self.logger.warning(e)
                continue
            self.__cache[cc.uuid] = cc

    def _get_capture_dir(self, uuid: str) -> Path:
        # Try to get from the recent captures cache in redis
        capture_dir = self.redis.hget('lookup_dirs', uuid)
        if capture_dir:
            to_return = Path(capture_dir)
            if to_return.exists():
                return to_return
            # The capture was either removed or archived, cleaning up
            self.redis.hdel('lookup_dirs', uuid)
            self.redis.delete(capture_dir)

        # Try to get from the archived captures cache in redis
        capture_dir = self.redis.hget('lookup_dirs_archived', uuid)
        if capture_dir:
            to_return = Path(capture_dir)
            if to_return.exists():
                return to_return
            # The capture was removed, remove the UUID
            self.redis.hdel('lookup_dirs_archived', uuid)
            self.redis.delete(capture_dir)
            self.logger.warning(f'UUID ({uuid}) linked to a missing directory ({capture_dir}).')
            raise MissingCaptureDirectory(f'UUID ({uuid}) linked to a missing directory ({capture_dir}).')
        raise MissingUUID(f'Unable to find UUID {uuid}.')

    def _create_pickle(self, capture_dir: Path) -> CrawledTree:
        with (capture_dir / 'uuid').open() as f:
            uuid = f.read().strip()

        lock_file = capture_dir / 'lock'
        if try_make_file(lock_file):
            # Lock created, we can process
            with lock_file.open('w') as f:
                f.write(datetime.now().isoformat())
        else:
            # The pickle is being created somewhere else, wait until it's done.
            while lock_file.exists():
                time.sleep(5)
            return load_pickle_tree(capture_dir, capture_dir.stat().st_mtime)

        har_files = sorted(capture_dir.glob('*.har'))
        pickle_file = capture_dir / 'tree.pickle'
        try:
            tree = CrawledTree(har_files, uuid)
            self.__resolve_dns(tree)
            if self.contextualizer:
                self.contextualizer.contextualize_tree(tree)
        except Har2TreeError as e:
            raise NoValidHarFile(e)
        except RecursionError as e:
            raise NoValidHarFile(f'Tree too deep, probably a recursive refresh: {e}.\n Append /export to the URL to get the files.')
        else:
            with pickle_file.open('wb') as _p:
                # Some pickles require a pretty high recursion limit, this kindof fixes it.
                # If the capture is really broken (generally a refresh to self), the capture
                # is discarded in the RecursionError above.
                default_recursion_limit = sys.getrecursionlimit()
                sys.setrecursionlimit(int(default_recursion_limit * 1.1))
                try:
                    pickle.dump(tree, _p)
                except RecursionError as e:
                    raise NoValidHarFile(f'Tree too deep, probably a recursive refresh: {e}.\n Append /export to the URL to get the files.')
                sys.setrecursionlimit(default_recursion_limit)
        finally:
            lock_file.unlink(missing_ok=True)
        return tree

    def _set_capture_cache(self, capture_dir: Path, tree: Optional[CrawledTree]=None) -> CaptureCache:
        '''Populate the redis cache for a capture. Mostly used on the index page.
        NOTE: Doesn't require the pickle.'''
        with (capture_dir / 'uuid').open() as f:
            uuid = f.read().strip()

        cache: Dict[str, Union[str, int]] = {'uuid': uuid, 'capture_dir': str(capture_dir)}
        if (capture_dir / 'error.txt').exists():
            # Something went wrong
            with (capture_dir / 'error.txt').open() as _error:
                content = _error.read()
                try:
                    error_to_cache = json.loads(content)
                    if isinstance(error_to_cache, dict) and error_to_cache.get('details'):
                        error_to_cache = error_to_cache.get('details')
                except json.decoder.JSONDecodeError:
                    # old format
                    error_to_cache = content
                cache['error'] = f'The capture {capture_dir.name} has an error: {error_to_cache}'

        if (har_files := sorted(capture_dir.glob('*.har'))):
            try:
                har = HarFile(har_files[0], uuid)
                cache['title'] = har.initial_title
                cache['timestamp'] = har.initial_start_time
                cache['url'] = har.root_url
                if har.initial_redirects and har.need_tree_redirects:
                    if not tree:
                        # try to load tree from disk
                        tree = load_pickle_tree(capture_dir, capture_dir.stat().st_mtime)
                    # get redirects
                    if tree:
                        cache['redirects'] = json.dumps(tree.redirects)
                        cache['incomplete_redirects'] = 0
                    else:
                        # Pickle not available
                        cache['redirects'] = json.dumps(har.initial_redirects)
                        cache['incomplete_redirects'] = 1
                else:
                    cache['redirects'] = json.dumps(har.initial_redirects)
                    cache['incomplete_redirects'] = 0

            except Har2TreeError as e:
                cache['error'] = str(e)
        else:
            cache['error'] = f'No har files in {capture_dir.name}'

        if (cache.get('error')
                and isinstance(cache['error'], str)
                and 'HTTP Error' not in cache['error']):
            self.logger.warning(cache['error'])

        if (capture_dir / 'categories').exists():
            with (capture_dir / 'categories').open() as _categories:
                cache['categories'] = json.dumps([c.strip() for c in _categories.readlines()])

        if (capture_dir / 'no_index').exists():
            # If the folders claims anonymity
            cache['no_index'] = 1

        if (capture_dir / 'parent').exists():
            # The capture was initiated from an other one
            with (capture_dir / 'parent').open() as f:
                cache['parent'] = f.read().strip()

        p = self.redis.pipeline()
        p.hset('lookup_dirs', uuid, str(capture_dir))
        p.hmset(str(capture_dir), cache)  # type: ignore
        p.execute()
        return CaptureCache(cache)

    def __resolve_dns(self, ct: CrawledTree):
        '''Resolves all domains of the tree, keeps A (IPv4), AAAA (IPv6), and CNAME entries
        and store them in ips.json and cnames.json, in the capture directory.
        Updates the nodes of the tree accordingly so the information is available.
        '''

        def _build_cname_chain(known_cnames: Dict[str, Optional[str]], hostname) -> List[str]:
            '''Returns a list of CNAMEs starting from one hostname.
            The CNAMEs resolutions are made in `_resolve_dns`. A hostname can have a CNAME entry
            and the CNAME entry can have an other CNAME entry, and so on multiple times.
            This method loops over the hostnames until there are no CNAMES.'''
            cnames: List[str] = []
            to_search = hostname
            while True:
                if known_cnames.get(to_search) is None:
                    break
                # At this point, known_cnames[to_search] must exist and be a str
                cnames.append(known_cnames[to_search])  # type: ignore
                to_search = known_cnames[to_search]
            return cnames

        cnames_path = ct.root_hartree.har.path.parent / 'cnames.json'
        ips_path = ct.root_hartree.har.path.parent / 'ips.json'
        host_cnames: Dict[str, Optional[str]] = {}
        if cnames_path.exists():
            try:
                with cnames_path.open() as f:
                    host_cnames = json.load(f)
            except json.decoder.JSONDecodeError:
                # The json is broken, delete and re-trigger the requests
                host_cnames = {}

        host_ips: Dict[str, List[str]] = {}
        if ips_path.exists():
            try:
                with ips_path.open() as f:
                    host_ips = json.load(f)
            except json.decoder.JSONDecodeError:
                # The json is broken, delete and re-trigger the requests
                host_ips = {}

        for node in ct.root_hartree.hostname_tree.traverse():
            if node.name not in host_cnames or node.name not in host_ips:
                # Resolve and cache
                try:
                    response = dns.resolver.resolve(node.name, search=True)
                    for answer in response.response.answer:
                        if answer.rdtype == dns.rdatatype.RdataType.CNAME:
                            host_cnames[str(answer.name).rstrip('.')] = str(answer[0].target).rstrip('.')
                        else:
                            host_cnames[str(answer.name).rstrip('.')] = None

                        if answer.rdtype in [dns.rdatatype.RdataType.A, dns.rdatatype.RdataType.AAAA]:
                            host_ips[str(answer.name).rstrip('.')] = list({str(b) for b in answer})
                except Exception:
                    host_cnames[node.name] = None
                    host_ips[node.name] = []
            cnames = _build_cname_chain(host_cnames, node.name)
            if cnames:
                node.add_feature('cname', cnames)
                if cnames[-1] in host_ips:
                    node.add_feature('resolved_ips', host_ips[cnames[-1]])
            elif node.name in host_ips:
                node.add_feature('resolved_ips', host_ips[node.name])

        with cnames_path.open('w') as f:
            json.dump(host_cnames, f)
        with ips_path.open('w') as f:
            json.dump(host_ips, f)
        return ct
