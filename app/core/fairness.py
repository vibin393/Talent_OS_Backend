"""Per-tenant queue fairness (NFR-16; Epic 12 A9).

Bulk AI and import tasks take a per-tenant counting semaphore (``BULK_CONCURRENCY_PER_TENANT`` slots) so one
tenant's bulk work cannot occupy every worker. A task that cannot take a slot retries later with a jittered
countdown (``retry_countdown``). Slots are Redis keys with a TTL (a crashed worker frees its slot when the
lease expires); an in-process fallback is used when Redis is unavailable and in eager mode.
"""

import contextlib
import random
import threading
import time
import uuid
from collections.abc import AsyncIterator

from loguru import logger

from app.core.settings import get_settings

LEASE_SECONDS = 15 * 60
_local: dict[str, dict[str, float]] = {}
_lock = threading.Lock()


class SlotUnavailable(Exception):  # ruff: ignore[error-suffix-on-exception-name] - control-flow signal
    pass


def retry_countdown(attempt: int = 0) -> int:
    """Jittered backoff (seconds) for a task that could not take a slot."""
    return int(min(300, 5 * 2 ** min(attempt, 6)) * random.uniform(0.5, 1.5))


def _local_acquire(key: str, token: str, limit: int) -> bool:
    now = time.monotonic()
    with _lock:
        slots = {t: exp for t, exp in _local.get(key, {}).items() if exp > now}
        if len(slots) >= limit:
            _local[key] = slots
            return False
        slots[token] = now + LEASE_SECONDS
        _local[key] = slots
        return True


def _local_release(key: str, token: str) -> None:
    with _lock:
        _local.get(key, {}).pop(token, None)


async def _redis_acquire(key: str, token: str, limit: int) -> bool:
    from redis.asyncio import Redis

    s = get_settings()
    client = Redis(host=s.redis_host, port=s.redis_port, db=2, password=s.redis_password or None, socket_timeout=0.5)
    try:
        now = time.time()
        await client.zremrangebyscore(key, 0, now)
        await client.zadd(key, {token: now + LEASE_SECONDS})
        rank = await client.zrank(key, token)
        await client.expire(key, LEASE_SECONDS)
        if rank is None or rank >= limit:
            await client.zrem(key, token)
            return False
        return True
    finally:
        await client.aclose()


async def _redis_release(key: str, token: str) -> None:
    from redis.asyncio import Redis

    s = get_settings()
    client = Redis(host=s.redis_host, port=s.redis_port, db=2, password=s.redis_password or None, socket_timeout=0.5)
    try:
        await client.zrem(key, token)
    finally:
        await client.aclose()


@contextlib.asynccontextmanager
async def tenant_slot(tenant_id: uuid.UUID, kind: str = "bulk", limit: int | None = None) -> AsyncIterator[None]:
    """Hold one of the tenant's bulk slots; raises ``SlotUnavailable`` when all are taken. ``limit`` overrides
    ``BULK_CONCURRENCY_PER_TENANT`` for this ``kind`` (Epic 11: async exports, 2 per tenant)."""
    s = get_settings()
    key = f"fair:{kind}:{tenant_id}"
    token = uuid.uuid4().hex
    limit = limit or s.bulk_concurrency_per_tenant
    use_redis = s.rate_limit_backend == "redis" and not s.celery_task_always_eager
    acquired_remote = False
    if use_redis:
        try:
            acquired_remote = await _redis_acquire(key, token, limit)
            if not acquired_remote:
                raise SlotUnavailable(key)
        except SlotUnavailable:
            raise
        except Exception as exc:
            logger.warning("Fairness semaphore: Redis unavailable ({}); using in-process slots", type(exc).__name__)
            use_redis = False
    if not use_redis and not _local_acquire(key, token, limit):
        raise SlotUnavailable(key)
    try:
        yield
    finally:
        if acquired_remote:
            try:
                await _redis_release(key, token)
            except Exception:
                logger.warning("Fairness semaphore: could not release {}", key)
        else:
            _local_release(key, token)
