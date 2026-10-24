"""Tiny key-value store for OAuth state (single use, 10 min) and the free/busy cache (2 min).

Redis (``SET NX EX`` / ``GETDEL``) with an in-process fallback when Redis is unreachable (a warning is logged
once). Tests install ``MemoryStore`` with ``set_store``.
"""

import contextlib
import threading
import time
from typing import Protocol

from loguru import logger

from app.core.settings import get_settings


class Store(Protocol):
    async def put(self, key: str, value: str, ttl: int) -> None: ...

    async def get(self, key: str) -> str | None: ...

    async def pop(self, key: str) -> str | None: ...


class MemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    async def put(self, key: str, value: str, ttl: int) -> None:  # NOSONAR
        with self._lock:
            self._data[key] = (value, time.monotonic() + ttl)

    def _live(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        if item[1] < time.monotonic():
            self._data.pop(key, None)
            return None
        return item[0]

    async def get(self, key: str) -> str | None:  # NOSONAR
        with self._lock:
            return self._live(key)

    async def pop(self, key: str) -> str | None:  # NOSONAR
        with self._lock:
            value = self._live(key)
            self._data.pop(key, None)
            return value


class RedisStore:
    def __init__(self) -> None:
        from redis.asyncio import Redis

        s = get_settings()
        self._redis = Redis(
            host=s.redis_host,
            port=s.redis_port,
            password=s.redis_password or None,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )

    async def put(self, key: str, value: str, ttl: int) -> None:
        await self._redis.set(key, value, ex=ttl)

    async def get(self, key: str) -> str | None:
        value = await self._redis.get(key)
        return None if value is None else str(value)

    async def pop(self, key: str) -> str | None:
        value = await self._redis.getdel(key)
        return None if value is None else str(value)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._redis.aclose()


_override: Store | None = None
_fallback = MemoryStore()
_warned = False


def set_store(store: Store | None) -> None:
    global _override  # process-wide wiring (tests)
    _override = store


class _Resilient:
    """Redis first; the in-memory store when Redis fails."""

    async def _call(self, method: str, *args: object) -> str | None:
        global _warned  # warn once per process
        try:
            store = RedisStore()  # per call: Celery tasks run on their own event loops
            try:
                return await getattr(store, method)(*args)  # type: ignore[no-any-return]
            finally:
                await store.close()
        except Exception:
            if not _warned:
                logger.warning("Redis unavailable for connection state; using the in-process store")
                _warned = True
            return await getattr(_fallback, method)(*args)  # type: ignore[no-any-return]

    async def put(self, key: str, value: str, ttl: int) -> None:
        await self._call("put", key, value, ttl)

    async def get(self, key: str) -> str | None:
        return await self._call("get", key)

    async def pop(self, key: str) -> str | None:
        return await self._call("pop", key)


def get_store() -> Store:
    return _override if _override is not None else _Resilient()
