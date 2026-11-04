"""Session, profile and summary cache.

Redis is the production backend: conversation snapshots, customer profiles and
rolling conversation summaries survive an application restart and are shared by
every window of the same customer. When no server is reachable the cache
degrades to an in-process dict, so a demo or a test run never needs Redis and
never silently loses correctness (only the shared-across-process property).

Keys are namespaced per data directory, so two workspaces (or 139 test cases)
never see each other's snapshots on the same server.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


class BaseCache:
    backend = "memory"
    degraded = False

    def __init__(self, namespace: str = "") -> None:
        self.namespace = namespace

    def k(self, *parts: Any) -> str:
        return ":".join(["pa", self.namespace, *(str(part) for part in parts)])

    def get_json(self, name: str, default=None):
        raw = self._get(name)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    def set_json(self, name: str, value, ttl: int | None = None) -> None:
        self._set(name, json.dumps(value, ensure_ascii=False, sort_keys=True), ttl)

    def keys(self, prefix: str) -> list[str]:
        return self._keys(prefix)

    def invalidate(self, prefix: str) -> int:
        return self._delete_prefix(prefix)

    def close(self) -> None:
        pass

    def info(self) -> dict:
        return {"backend": self.backend, "degraded": self.degraded, "namespace": self.namespace}

    def _get(self, name: str) -> str | None:
        raise NotImplementedError

    def _set(self, name: str, value: str, ttl: int | None) -> None:
        raise NotImplementedError

    def _keys(self, prefix: str) -> list[str]:
        raise NotImplementedError

    def _delete_prefix(self, prefix: str) -> int:
        raise NotImplementedError


class MemoryCache(BaseCache):
    """Bounded, TTL-aware dict. Used by tests and as the Redis fallback."""

    def __init__(self, namespace: str = "", limit: int = 4096) -> None:
        super().__init__(namespace)
        self.backend = "memory"
        self._data: dict[str, tuple[str, float | None]] = {}
        self._lock = threading.Lock()
        self._limit = limit

    @staticmethod
    def _expired(item) -> bool:
        return item[1] is not None and item[1] < time.time()

    def _get(self, name: str) -> str | None:
        with self._lock:
            item = self._data.get(name)
            if item is None:
                return None
            if self._expired(item):
                self._data.pop(name, None)
                return None
            return item[0]

    def _set(self, name: str, value: str, ttl: int | None) -> None:
        with self._lock:
            if len(self._data) >= self._limit:
                oldest = next(iter(self._data))
                self._data.pop(oldest, None)
            self._data[name] = (value, time.time() + ttl if ttl else None)

    def _keys(self, prefix: str) -> list[str]:
        with self._lock:
            alive = [name for name, item in self._data.items() if not self._expired(item)]
            return [name for name in alive if name.startswith(prefix)]

    def _delete_prefix(self, prefix: str) -> int:
        with self._lock:
            victims = [name for name in self._data if name.startswith(prefix)]
            for name in victims:
                self._data.pop(name, None)
            return len(victims)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class RedisCache(BaseCache):
    """Thin JSON cache over redis-py; every failure degrades instead of raising."""

    def __init__(self, url: str, *, namespace: str = "", timeout: float = 1.0) -> None:
        super().__init__(namespace)
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        self.backend = "redis"
        self.url = url
        self._client = redis.Redis.from_url(
            url, decode_responses=True, encoding_errors="replace", socket_timeout=timeout,
            socket_connect_timeout=timeout, health_check_interval=30,
            retry=Retry(NoBackoff(), 0), retry_on_error=[], protocol=2,
        )
        self._client.ping()

    def _get(self, name: str) -> str | None:
        try:
            return self._client.get(name)
        except Exception as exc:
            logger.warning("Redis GET failed: %s", exc)
            self.degraded = True
            return None

    def _set(self, name: str, value: str, ttl: int | None) -> None:
        try:
            self._client.set(name, value, ex=ttl)
        except Exception as exc:
            logger.warning("Redis SET failed: %s", exc)
            self.degraded = True

    def _keys(self, prefix: str) -> list[str]:
        try:
            return list(self._client.scan_iter(match=prefix + "*", count=200))
        except Exception as exc:
            logger.warning("Redis SCAN failed: %s", exc)
            self.degraded = True
            return []

    def _delete_prefix(self, prefix: str) -> int:
        removed = 0
        try:
            batch = []
            for name in self._client.scan_iter(match=prefix + "*", count=200):
                batch.append(name)
                if len(batch) >= 200:
                    removed += self._client.delete(*batch)
                    batch.clear()
            if batch:
                removed += self._client.delete(*batch)
        except Exception as exc:
            logger.warning("Redis SCAN/DEL failed: %s", exc)
            self.degraded = True
        return removed

    def server_version(self) -> str:
        try:
            return str(self._client.info("server").get("redis_version", "?"))
        except Exception:
            return "?"

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


_PROBE: dict[str, tuple[bool, float]] = {}
_PROBE_TTL = 5.0


def namespace_for(path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:10]


def make_cache(settings) -> BaseCache:
    url = (getattr(settings, "redis_url", "") or "").strip()
    namespace = namespace_for(getattr(settings, "data_dir", settings.root))
    if url:
        ok, checked = _PROBE.get(url, (True, 0.0))
        if ok or (time.time() - checked) > _PROBE_TTL:
            for _attempt in range(2):
                try:
                    cache = RedisCache(url, namespace=namespace, timeout=5.0)
                    _PROBE[url] = (True, time.time())
                    logger.info("Redis cache enabled at %s (version %s)", url, cache.server_version())
                    return cache
                except Exception as exc:
                    _PROBE[url] = (False, time.time())
                    last = exc
            logger.warning("Redis unavailable (%s: %s); using in-process cache", type(last).__name__, last)
    cache = MemoryCache(namespace=namespace)
    cache.degraded = True
    return cache
