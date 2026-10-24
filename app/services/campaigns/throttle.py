# ruff: file-ignore[line-too-long]
"""Per-(tenant, channel) token bucket + daily counter (Epic 39 A4, NFR-34, AD-54).

Redis keys ``mkt:tb:<tenant>:<channel>`` (bucket: tokens + last refill, rate = tenant ``rate_per_minute``, burst =
rate) and ``mkt:day:<tenant>:<channel>:<yyyymmdd>`` (daily counter vs ``daily_cap``), both updated atomically by one
Lua script. Tests and a Redis outage use the in-process backend with the same algorithm (``RATE_LIMIT_BACKEND``).
"""

import asyncio
import dataclasses
import threading
import time
import uuid
import weakref
from datetime import UTC, datetime
from typing import Protocol

from loguru import logger

from app.core.settings import get_settings

_LUA = """
local rate = tonumber(ARGV[1])
local cap = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local day = tonumber(redis.call('GET', KEYS[2]) or '0')
if day >= cap then
  return {0, -1}
end
local tokens = tonumber(redis.call('HGET', KEYS[1], 't') or ARGV[1])
local ts = tonumber(redis.call('HGET', KEYS[1], 'ts') or ARGV[3])
tokens = math.min(rate, tokens + (now - ts) * rate / 60000)
if tokens < 1 then
  redis.call('HSET', KEYS[1], 't', tokens, 'ts', now)
  redis.call('PEXPIRE', KEYS[1], 120000)
  return {0, math.ceil((1 - tokens) * 60000 / rate)}
end
redis.call('HSET', KEYS[1], 't', tokens - 1, 'ts', now)
redis.call('PEXPIRE', KEYS[1], 120000)
redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[2], 172800)
return {1, 0}
"""


@dataclasses.dataclass(frozen=True, slots=True)
class Take:
    allowed: bool
    retry_after_ms: int = 0
    daily_exhausted: bool = False


class Backend(Protocol):
    async def take(self, bucket: str, day_key: str, rate: int, cap: int, now_ms: int) -> tuple[int, int]: ...


class MemoryBackend:
    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, int]] = {}
        self._days: dict[str, int] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._days.clear()

    async def take(self, bucket: str, day_key: str, rate: int, cap: int, now_ms: int) -> tuple[int, int]:  # NOSONAR
        with self._lock:
            if self._days.get(day_key, 0) >= cap:
                return 0, -1
            tokens, ts = self._buckets.get(bucket, (float(rate), now_ms))
            tokens = min(float(rate), tokens + (now_ms - ts) * rate / 60000)
            if tokens < 1:
                self._buckets[bucket] = (tokens, now_ms)
                return 0, int((1 - tokens) * 60000 / rate) + 1
            self._buckets[bucket] = (tokens - 1, now_ms)
            self._days[day_key] = self._days.get(day_key, 0) + 1
            return 1, 0


class RedisBackend:
    def __init__(self) -> None:
        self._clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, object] = (
            weakref.WeakKeyDictionary()
        )

    def _client(self) -> object:
        from redis.asyncio import Redis

        loop = asyncio.get_running_loop()
        if loop not in self._clients:
            s = get_settings()
            self._clients[loop] = Redis(
                host=s.redis_host,
                port=s.redis_port,
                db=2,
                password=s.redis_password or None,
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
        return self._clients[loop]

    async def take(self, bucket: str, day_key: str, rate: int, cap: int, now_ms: int) -> tuple[int, int]:
        allowed, wait = await self._client().eval(_LUA, 2, bucket, day_key, rate, cap, now_ms)  # type: ignore[attr-defined]
        return int(allowed), int(wait)


_memory = MemoryBackend()
_redis: RedisBackend | None = None
_override: Backend | None = None


def memory_backend() -> MemoryBackend:
    return _memory


def set_backend(backend: Backend | None) -> None:
    global _override  # test hook, process-wide
    _override = backend


def _backend() -> Backend:
    global _redis  # lazily created, process-wide
    if _override is not None:
        return _override
    if get_settings().rate_limit_backend == "memory":
        return _memory
    if _redis is None:
        _redis = RedisBackend()
    return _redis


async def take(
    tenant_id: uuid.UUID, channel: str, *, rate: int, daily_cap: int, today: str | None = None
) -> Take:
    day = today or datetime.now(UTC).strftime("%Y%m%d")
    bucket = f"mkt:tb:{tenant_id}:{channel}"
    day_key = f"mkt:day:{tenant_id}:{channel}:{day}"
    now_ms = int(time.time() * 1000)
    backend = _backend()
    try:
        allowed, wait = await backend.take(bucket, day_key, max(1, rate), max(1, daily_cap), now_ms)
    except Exception as exc:
        if backend is _memory:
            raise
        logger.warning("Marketing throttle: Redis unavailable ({}); in-memory fallback", type(exc).__name__)
        allowed, wait = await _memory.take(bucket, day_key, max(1, rate), max(1, daily_cap), now_ms)
    if allowed:
        return Take(True)
    if wait < 0:
        return Take(False, 0, daily_exhausted=True)
    return Take(False, wait)
