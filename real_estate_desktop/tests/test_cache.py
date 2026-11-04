import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from real_estate_desktop.cache import MemoryCache, RedisCache, make_cache

REDIS_URL = 'redis://127.0.0.1:6379/0'


def redis_available() -> bool:
    try:
        RedisCache(REDIS_URL, namespace='probe')
        return True
    except Exception:
        return False


class MemoryBackend(unittest.TestCase):
    def test_json_roundtrip(self):
        cache = MemoryCache('ns')
        cache.set_json(cache.k('sess', 'b', 'c', 's'), {'a': 1, 'cn': '预算'})
        self.assertEqual(cache.get_json(cache.k('sess', 'b', 'c', 's'))['cn'], '预算')

    def test_missing_key_returns_default(self):
        self.assertEqual(MemoryCache('ns').get_json('nope', {'x': 1}), {'x': 1})

    def test_ttl_expires(self):
        cache = MemoryCache('ns')
        cache.set_json(cache.k('k'), 1, ttl=1)
        self.assertEqual(cache.get_json(cache.k('k')), 1)
        time.sleep(1.1)
        self.assertIsNone(cache.get_json(cache.k('k')))

    def test_prefix_invalidate_removes_only_that_customer(self):
        cache = MemoryCache('ns')
        cache.set_json(cache.k('sess', 'b', 'c1', 's1'), 1)
        cache.set_json(cache.k('sess', 'b', 'c1', 's2'), 2)
        cache.set_json(cache.k('sess', 'b', 'c2', 's1'), 3)
        self.assertEqual(cache.invalidate(cache.k('sess', 'b', 'c1')), 2)
        self.assertEqual(len(cache.keys(cache.k('sess', 'b'))), 1)

    def test_namespaces_isolate_workspaces(self):
        one, two = MemoryCache('one'), MemoryCache('two')
        one.set_json(one.k('sess', 'b', 'c', 's'), 'x')
        self.assertIsNone(two.get_json(two.k('sess', 'b', 'c', 's')))


class Factory(unittest.TestCase):
    def settings(self, url):
        return SimpleNamespace(redis_url=url, data_dir=Path('D:/tmp/pa-cache-test'), root=Path('D:/tmp'))

    def test_unreachable_redis_degrades_to_memory(self):
        cache = make_cache(self.settings('redis://127.0.0.1:6399/0'))
        self.assertEqual(cache.backend, 'memory')
        self.assertTrue(cache.degraded)

    @unittest.skipUnless(redis_available(), '本地未运行 Redis')
    def test_redis_backend_roundtrip(self):
        cache = make_cache(self.settings(REDIS_URL))
        self.assertEqual(cache.backend, 'redis')
        cache.set_json(cache.k('sess', 'b', 'c', 's'), {'a': 1}, ttl=30)
        self.assertEqual(cache.get_json(cache.k('sess', 'b', 'c', 's')), {'a': 1})
        cache.invalidate(cache.k('sess', 'b', 'c'))
        self.assertIsNone(cache.get_json(cache.k('sess', 'b', 'c', 's')))
        cache.close()


if __name__ == '__main__':
    unittest.main()
