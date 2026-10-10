# ruff: noqa: E501, D401
"""Run concurrency semaphores (Epic 53, AD-58, story 53.9).

Two counting semaphores per running run: ``agents:sem:<tenant>`` (<= ``agent_max_concurrent_runs_per_tenant``) and
``agents:sem:<tenant>:<agent>:<subject>`` (<= ``agent_max_concurrent_runs_per_agent_subject``). The run id is the
member, so re-acquiring on the next step is idempotent; entries expire with the lease (a crashed worker frees its slot)
and are released when the run waits or ends. Redis sorted sets (db 2) with an in-process fallback; bypassed under
``CELERY_TASK_ALWAYS_EAGER`` for deterministic local / test runs (``force_local()`` enables the in-process backend).
"""

import threading
import time
import uuid

from loguru import logger

from app.core.settings import get_settings

_lock = threading.Lock()
_local: dict[str, dict[str, float]] = {}
_forced = False


def force_local(on: bool = True) -> None:
    """Tests: use the in-process semaphores even in eager mode."""
    global _forced  # test switch
    _forced = on
    with _lock:
        _local.clear()


def _keys(tenant_id: uuid.UUID, agent_key: str, subject: str) -> list[tuple[str, int]]:
    s = get_settings()
    return [
        (f"agents:sem:{tenant_id}", s.agent_max_concurrent_runs_per_tenant),
        (f"agents:sem:{tenant_id}:{agent_key}:{subject}", s.agent_max_concurrent_runs_per_agent_subject),
    ]


def _enabled() -> tuple[bool, bool]:
    """``(enabled, use_redis)``."""
    s = get_settings()
    if _forced:
        return True, False
    if s.celery_task_always_eager:
        return False, False
    return True, s.rate_limit_backend == "redis"


def _local_acquire(key: str, token: str, limit: int, ttl: float) -> bool:
    now = time.time()
    with _lock:
        slots = {t: exp for t, exp in _local.get(key, {}).items() if exp > now}
        if token not in slots and len(slots) >= limit:
            _local[key] = slots
            return False
        slots[token] = now + ttl
        _local[key] = slots
        return True


def _local_release(key: str, token: str) -> None:
    with _lock:
        _local.get(key, {}).pop(token, None)


async def _redis(fn_name: str, key: str, token: str, limit: int, ttl: float) -> bool:
    from redis.asyncio import Redis

    s = get_settings()
    client = Redis(
        host=s.redis_host, port=s.redis_port, db=2, password=s.redis_password or None, socket_timeout=0.5
    )
    try:
        if fn_name == "release":
            await client.zrem(key, token)
            return True
        now = time.time()
        await client.zremrangebyscore(key, 0, now)
        existing = await client.zscore(key, token)
        await client.zadd(key, {token: now + ttl})
        await client.expire(key, int(ttl) + 60)
        if existing is not None:
            return True
        rank = await client.zrank(key, token)
        if rank is None or rank >= limit:
            await client.zrem(key, token)
            return False
        return True
    finally:
        await client.aclose()


async def acquire(tenant_id: uuid.UUID, agent_key: str, subject: str, run_id: uuid.UUID, ttl: float) -> bool:
    enabled, use_redis = _enabled()
    if not enabled:
        return True
    token = str(run_id)
    taken: list[str] = []
    for key, limit in _keys(tenant_id, agent_key, subject):
        ok = False
        if use_redis:
            try:
                ok = await _redis("acquire", key, token, limit, ttl)
            except Exception as exc:
                logger.warning(
                    "Agent semaphore: Redis unavailable ({}); using in-process slots", type(exc).__name__
                )
                use_redis = False
        if not use_redis:
            ok = _local_acquire(key, token, limit, ttl)
        if not ok:
            for k in taken:
                await _release_key(k, token, use_redis)
            return False
        taken.append(key)
    return True


async def _release_key(key: str, token: str, use_redis: bool) -> None:
    if use_redis:
        try:
            await _redis("release", key, token, 0, 0)
            return
        except Exception:
            logger.warning("Agent semaphore: could not release {}", key)
    _local_release(key, token)


async def release(tenant_id: uuid.UUID, agent_key: str, subject: str, run_id: uuid.UUID) -> None:
    enabled, use_redis = _enabled()
    if not enabled:
        return
    for key, _ in _keys(tenant_id, agent_key, subject):
        await _release_key(key, str(run_id), use_redis)


__all__ = ["acquire", "force_local", "release"]
