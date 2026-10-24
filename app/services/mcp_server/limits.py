# ruff: file-ignore[line-too-long]
"""MCP rate limits and caps (Epic 73, spec 0.6; FR-379). Counters live in Redis db 2 (``RATE_LIMIT_BACKEND=memory`` ->
in-process, as ``core.rate_limit``; an unreachable Redis falls back to memory with a warning).

| Limit | Key | Window |
|---|---|---|
| calls per session per minute (tier, lowered by the tenant) | ``core.rate_limit`` policy ``mcp_min`` | sliding 60 s |
| calls per user per UTC day | ``mcp:rl:day:<tenant>:<user>:<date>`` | fixed UTC day |
| proposals per session per hour | ``mcp:rl:prop:<session>:<hour>`` | fixed UTC hour |
| distinct candidates per session-hour (detector) | ``mcp:cand:<session>:<hour>`` (a set) | fixed UTC hour |
"""

import asyncio
import dataclasses
import threading
import time
import weakref
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from app.core import rate_limit
from app.core.settings import get_settings


class _MemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._data.clear()

    def _live(self, key: str, now: float) -> Any:
        hit = self._data.get(key)
        if hit is None or hit[1] <= now:
            self._data.pop(key, None)
            return None
        return hit[0]

    async def get(self, key: str) -> int:  # NOSONAR
        with self._lock:
            value = self._live(key, time.monotonic())
            return int(value or 0) if not isinstance(value, set) else len(value)

    async def incr(self, key: str, ttl: int) -> int:  # NOSONAR
        now = time.monotonic()
        with self._lock:
            value = int(self._live(key, now) or 0) + 1
            self._data[key] = (value, now + ttl)
            return value

    async def sadd(self, key: str, members: Iterable[str], ttl: int) -> int:  # NOSONAR
        now = time.monotonic()
        with self._lock:
            current = self._live(key, now)
            current = set(current) if isinstance(current, set) else set()
            current.update(members)
            self._data[key] = (current, now + ttl)
            return len(current)


class _RedisStore:
    def __init__(self) -> None:
        self._clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any] = weakref.WeakKeyDictionary()

    def _client(self) -> Any:
        from redis.asyncio import Redis

        loop = asyncio.get_running_loop()
        if loop not in self._clients:
            s = get_settings()
            self._clients[loop] = Redis(
                host=s.redis_host, port=s.redis_port, db=2, password=s.redis_password or None, decode_responses=True,
                socket_connect_timeout=0.5, socket_timeout=0.5,
            )  # fmt: skip
        return self._clients[loop]

    async def get(self, key: str) -> int:
        client = self._client()
        kind = await client.type(key)
        if kind == "set":
            return int(await client.scard(key))
        return int(await client.get(key) or 0)

    async def incr(self, key: str, ttl: int) -> int:
        client = self._client()
        value = int(await client.incr(key))
        if value == 1:
            await client.expire(key, ttl)
        return value

    async def sadd(self, key: str, members: Iterable[str], ttl: int) -> int:
        client = self._client()
        items = list(members)
        if items:
            await client.sadd(key, *items)
            await client.expire(key, ttl)
        return int(await client.scard(key))


_memory = _MemoryStore()
_redis: _RedisStore | None = None
_warned = {"at": 0.0}


def memory_store() -> _MemoryStore:
    return _memory


async def _call(op: str, *args: Any) -> int:
    global _redis  # lazily created, process-wide
    if get_settings().rate_limit_backend == "memory":
        return await getattr(_memory, op)(*args)  # type: ignore[no-any-return]
    if _redis is None:
        _redis = _RedisStore()
    try:
        return await getattr(_redis, op)(*args)  # type: ignore[no-any-return]
    except Exception as exc:  # Redis down: keep limiting in-process
        if time.time() - _warned["at"] > 60:
            _warned["at"] = time.time()
            logger.warning("MCP limits: Redis unavailable ({}); using in-memory counters", type(exc).__name__)
        return await getattr(_memory, op)(*args)  # type: ignore[no-any-return]


def _now() -> datetime:
    return datetime.now(UTC)


def _hour(now: datetime) -> str:
    return now.strftime("%Y%m%d%H")


@dataclasses.dataclass(frozen=True, slots=True)
class Refusal:
    text: str
    retry_after: int | None = None


async def check_minute(session_key: str, limit: int) -> Refusal | None:
    result = await rate_limit.check("mcp_min", session_key, limit, 60)
    if result.allowed:
        return None
    wait = max(1, result.retry_after)
    return Refusal(f"Rate limit reached for this session. Try again in {wait} seconds.", wait)


async def check_day(tenant_id: Any, user_id: Any, limit: int, *, now: datetime | None = None) -> Refusal | None:
    now = now or _now()
    key = f"mcp:rl:day:{tenant_id}:{user_id}:{now.strftime('%Y%m%d')}"
    if await _call("get", key) >= limit:
        return Refusal(f"Daily limit of {limit:,} tool calls reached. It resets at 00:00 UTC.")
    await _call("incr", key, 90_000)
    return None


async def check_proposals(session_key: str, limit: int, *, now: datetime | None = None) -> Refusal | None:
    key = f"mcp:rl:prop:{session_key}:{_hour(now or _now())}"
    if await _call("get", key) >= limit:
        return Refusal(
            f"This session has created {limit} proposals in the last hour. Review them in Talent OS before asking for more."
        )
    return None


async def add_proposal(session_key: str, *, now: datetime | None = None) -> int:
    return await _call("incr", f"mcp:rl:prop:{session_key}:{_hour(now or _now())}", 3_700)


async def add_candidates(session_key: str, ids: Iterable[Any], *, now: datetime | None = None) -> int:
    """Add candidate ids to the session-hour set; returns its size (the bulk-read detector input)."""
    return await _call("sadd", f"mcp:cand:{session_key}:{_hour(now or _now())}", [str(i) for i in ids], 3_700)


def seconds_to_midnight(now: datetime | None = None) -> int:
    now = now or _now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((tomorrow - now).total_seconds())


__all__ = [
    "Refusal",
    "add_candidates",
    "add_proposal",
    "check_day",
    "check_minute",
    "check_proposals",
    "memory_store",
    "seconds_to_midnight",
]
