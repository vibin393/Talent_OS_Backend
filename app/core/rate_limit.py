"""Sliding-window rate limiting (NFR-16; Epic 12 A9).

Counters live in Redis under ``rl:{policy}:{subject}:{bucket}`` (bucket = ``floor(now / window)``). A request
is allowed when ``previous * (1 - elapsed/window) + current < limit`` -- the weighted sliding-window
approximation -- evaluated and incremented atomically by one Lua script.

* ``MemoryBackend`` implements the same algorithm in-process: used by tests (``RATE_LIMIT_BACKEND=memory``)
  and as the fallback when Redis is unreachable (a warning is logged; requests are still served).
* ``check(policy, subject, limit, window)`` returns a ``RateResult``; ``enforce(...)`` raises 429
  ``rate_limited`` with ``Retry-After`` and ``X-RateLimit-*`` headers.
* ``hit(key, limit, window)`` is kept as the Epic 6 fixed-window call signature (alias onto the sliding
  window) so earlier callers keep working.
* Subjects that could carry personal data (emails) are hashed by the caller (``subject_hash``).
"""

import asyncio
import dataclasses
import hashlib
import math
import threading
import time
import weakref
from typing import Protocol

from loguru import logger

from app.core.errors import RateLimited
from app.core.settings import get_settings

_LUA = """
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local weight = tonumber(ARGV[3])
local c = tonumber(redis.call('GET', KEYS[1]) or '0')
local p = tonumber(redis.call('GET', KEYS[2]) or '0')
local est = p * weight + c
if est + 1 > limit then
  return {0, tostring(est)}
end
c = redis.call('INCR', KEYS[1])
redis.call('PEXPIRE', KEYS[1], window_ms * 2)
return {1, tostring(p * weight + c)}
"""


@dataclasses.dataclass(frozen=True, slots=True)
class RateResult:
    allowed: bool
    limit: int
    remaining: int
    reset_after: int  # seconds until the current bucket ends
    retry_after: int  # seconds to wait when refused (0 when allowed)

    def headers(self) -> dict[str, str]:
        out = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_after),
        }
        if not self.allowed:
            out["Retry-After"] = str(max(1, self.retry_after))
        return out


class Backend(Protocol):
    async def incr(
        self, cur: str, prev: str, limit: int, window_ms: int, weight: float
    ) -> tuple[bool, float]: ...


class MemoryBackend:
    """In-process sliding window (tests, Redis-down fallback). Thread-safe; expiry is lazy."""

    def __init__(self) -> None:
        self._counts: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()

    def _get(self, key: str, now: float) -> int:
        value = self._counts.get(key)
        if value is None or value[1] <= now:
            self._counts.pop(key, None)
            return 0
        return value[0]

    async def incr(  # NOSONAR
        self, cur: str, prev: str, limit: int, window_ms: int, weight: float
    ) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            c = self._get(cur, now)
            p = self._get(prev, now)
            est = p * weight + c
            if est + 1 > limit:
                return False, est
            self._counts[cur] = (c + 1, now + 2 * window_ms / 1000)
            return True, p * weight + c + 1


class RedisBackend:
    def __init__(self) -> None:
        # keyed by the loop object (not id(): ids are reused after a loop is collected)
        self._clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, object] = (
            weakref.WeakKeyDictionary()
        )

    def _client(self) -> object:
        from redis.asyncio import Redis

        loop_key = asyncio.get_running_loop()
        if loop_key not in self._clients:
            s = get_settings()
            self._clients[loop_key] = Redis(
                host=s.redis_host,
                port=s.redis_port,
                db=2,
                password=s.redis_password or None,
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
        return self._clients[loop_key]

    async def incr(
        self, cur: str, prev: str, limit: int, window_ms: int, weight: float
    ) -> tuple[bool, float]:
        client = self._client()
        allowed, est = await client.eval(_LUA, 2, cur, prev, limit, window_ms, weight)  # type: ignore[attr-defined]
        return bool(int(allowed)), float(est)


_memory = MemoryBackend()
_redis: RedisBackend | None = None
_override: Backend | None = None
_warned_at = 0.0


def memory_backend() -> MemoryBackend:
    return _memory


def set_backend(backend: Backend | None) -> None:
    """Test hook: force a backend (``None`` = follow ``RATE_LIMIT_BACKEND``)."""
    global _override  # process-wide wiring
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


def subject_hash(value: str) -> str:
    """Stable, non-reversible subject for personal identifiers (emails)."""
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:32]


async def check(policy: str, subject: str, limit: int, window_seconds: int) -> RateResult:
    """Count one request of ``subject`` under ``policy``; returns whether it is allowed."""
    global _warned_at  # throttles the fallback warning
    now = time.time()
    bucket = math.floor(now / window_seconds)
    elapsed = now - bucket * window_seconds
    weight = max(0.0, 1.0 - elapsed / window_seconds)
    cur = f"rl:{policy}:{subject}:{bucket}"
    prev = f"rl:{policy}:{subject}:{bucket - 1}"
    window_ms = window_seconds * 1000
    backend = _backend()
    try:
        allowed, est = await backend.incr(cur, prev, limit, window_ms, weight)
    except Exception as exc:
        if backend is _memory:
            raise
        if now - _warned_at > 60:
            _warned_at = now
            logger.warning(
                "Rate limiter: Redis unavailable ({}); using in-memory fallback", type(exc).__name__
            )
        allowed, est = await _memory.incr(cur, prev, limit, window_ms, weight)
    reset_after = max(1, math.ceil(window_seconds - elapsed))
    remaining = max(0, limit - math.ceil(est))
    retry_after = 0 if allowed else reset_after
    return RateResult(
        allowed=allowed, limit=limit, remaining=remaining, reset_after=reset_after, retry_after=retry_after
    )


def rate_limited(result: RateResult) -> RateLimited:
    exc = RateLimited("Too many requests; try again later", details={"retry_after": result.retry_after})
    headers = result.headers()

    def _apply(response: object) -> None:
        for key, value in headers.items():
            response.headers[key] = value  # type: ignore[attr-defined]

    exc.response_hooks.append(_apply)
    return exc


async def enforce(policy: str, subject: str, limit: int, window_seconds: int) -> RateResult:
    """``check`` and raise 429 ``rate_limited`` (with headers) when refused. No-op when disabled."""
    if not get_settings().rate_limit_enabled:
        return RateResult(True, limit, limit, window_seconds, 0)
    result = await check(policy, subject, limit, window_seconds)
    if not result.allowed:
        raise rate_limited(result)
    return result


async def hit(key: str, limit: int, window_seconds: int) -> bool:
    """Epic 6 compatibility alias: ``True`` when the request identified by ``key`` is within the limit."""
    if not get_settings().rate_limit_enabled:
        return True
    return (await check("legacy", key, limit, window_seconds)).allowed
