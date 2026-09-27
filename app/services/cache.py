import json
import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


class CacheBackend:
    async def get(self, key: str) -> Any | None:
        raise NotImplementedError

    async def set(self, key: str, value: Any, ttl: int) -> None:
        raise NotImplementedError

    async def delete_prefix(self, prefix: str) -> None:
        raise NotImplementedError

    async def ping(self) -> bool:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class MemoryCache(CacheBackend):
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, str]] = {}

    async def get(self, key: str) -> Any | None:
        import time

        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, raw = entry
        if expires_at < time.monotonic():
            self._store.pop(key, None)
            return None
        return json.loads(raw)

    async def set(self, key: str, value: Any, ttl: int) -> None:
        import time

        self._store[key] = (time.monotonic() + ttl, json.dumps(value, default=str))

    async def delete_prefix(self, prefix: str) -> None:
        for key in [key for key in self._store if key.startswith(prefix)]:
            self._store.pop(key, None)

    async def ping(self) -> bool:
        return True


class RedisCache(CacheBackend):
    def __init__(self, url: str) -> None:
        self._url = url
        self._client: Any | None = None

    async def _get_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        try:
            from redis.asyncio import Redis

            client = Redis.from_url(
                self._url, encoding="utf-8", decode_responses=True, socket_connect_timeout=1
            )
            await client.ping()
            self._client = client
            return client
        except Exception as exc:
            logger.warning("redis_unavailable", extra={"error": str(exc)})
            self._client = None
            return None

    async def get(self, key: str) -> Any | None:
        client = await self._get_client()
        if client is None:
            return None
        try:
            raw = await client.get(key)
        except Exception as exc:
            logger.warning("cache_get_failed", extra={"key": key, "error": str(exc)})
            return None
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        client = await self._get_client()
        if client is None:
            return
        try:
            await client.set(key, json.dumps(value, default=str), ex=ttl)
        except Exception as exc:
            logger.warning("cache_set_failed", extra={"key": key, "error": str(exc)})

    async def delete_prefix(self, prefix: str) -> None:
        client = await self._get_client()
        if client is None:
            return
        try:
            async for key in client.scan_iter(match=f"{prefix}*", count=200):
                await client.delete(key)
        except Exception as exc:
            logger.warning("cache_invalidate_failed", extra={"prefix": prefix, "error": str(exc)})

    async def ping(self) -> bool:
        client = await self._get_client()
        return client is not None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class NullCache(CacheBackend):
    async def get(self, key: str) -> Any | None:
        return None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        return None

    async def delete_prefix(self, prefix: str) -> None:
        return None

    async def ping(self) -> bool:
        return False


_backend: CacheBackend | None = None


def get_cache() -> CacheBackend:
    global _backend
    if _backend is None:
        _backend = RedisCache(settings.redis_url) if settings.redis_url else MemoryCache()
    return _backend


def set_cache(backend: CacheBackend) -> None:
    global _backend
    _backend = backend


async def cache_get(key: str) -> Any | None:
    return await get_cache().get(key)


async def cache_set(key: str, value: Any, ttl: int | None = None) -> None:
    await get_cache().set(key, value, ttl or settings.cache_ttl_seconds)


async def cache_invalidate(prefix: str) -> None:
    await get_cache().delete_prefix(prefix)


CACHE_PREFIX_CENTRES = "catalogue:centres"
CACHE_PREFIX_TESTS = "catalogue:tests"
