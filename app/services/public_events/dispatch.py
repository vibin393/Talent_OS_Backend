# ruff: file-ignore[line-too-long]
"""Outbox relay (Epic 66, AD-79): ``dispatch`` hands pending rows to every sink, ``sweep_tenant`` recovers stalled rows,
``kick`` (after commit) enqueues ``public_events.dispatch`` deduplicated per tenant for 2 s."""

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select, update

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.models.public_events import OutboxEvent
from app.services.public_events.outbox import to_row
from app.services.public_events.sinks import sinks

KICK_DEDUP_SECONDS = 2
SWEEP_AGE_SECONDS = 30


async def dispatch(tenant_id: uuid.UUID, *, batch: int = 500) -> int:
    """Lock up to ``batch`` pending rows (``FOR UPDATE SKIP LOCKED``, seq order), call every sink in its own SAVEPOINT and
    mark the rows dispatched only if every sink succeeded. A failing sink's SAVEPOINT is rolled back and the whole batch
    stays ``pending`` for the next run (sinks are idempotent). Returns the number of rows dispatched."""
    tid = uuid.UUID(str(tenant_id))
    async with tenant_session(tid) as db:
        locked = list(
            (
                await db.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.dispatch_status == "pending")
                    .order_by(OutboxEvent.seq)
                    .limit(batch)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        if not locked:
            return 0
        rows = [to_row(e) for e in locked]
        ctx = RequestContext(tenant_id=tid, actor_kind="system")
        ok = True
        for sink in sinks():
            selected = sink.select(rows)
            if not selected:
                continue
            try:
                async with db.begin_nested():
                    await sink.fn(ctx, db, selected)
            except Exception:
                ok = False
                logger.exception("outbox sink {} failed for tenant {}; batch stays pending", sink.name, tid)
        if not ok:
            return 0
        await db.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_([r.id for r in rows]))
            .values(dispatch_status="dispatched", dispatched_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        return len(rows)


async def has_stalled(tenant_id: uuid.UUID, *, older_than_seconds: int = SWEEP_AGE_SECONDS) -> bool:
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    async with tenant_session(tenant_id) as db:
        found = (
            await db.execute(
                select(OutboxEvent.id)
                .where(OutboxEvent.dispatch_status == "pending", OutboxEvent.created_at < cutoff)
                .limit(1)
            )
        ).first()
    return found is not None


async def sweep_tenant(
    tenant_id: uuid.UUID, *, older_than_seconds: int = SWEEP_AGE_SECONDS, batch: int = 500
) -> int:
    """Crash recovery: dispatch the tenant when it has pending rows older than ``older_than_seconds``."""
    if not await has_stalled(tenant_id, older_than_seconds=older_than_seconds):
        return 0
    total = 0
    while True:
        n = await dispatch(tenant_id, batch=batch)
        total += n
        if n < batch:
            return total


# ------------------------------------------------------------------ after-commit kick (deduplicated per tenant)
_local_lock = threading.Lock()
_local_until: dict[str, float] = {}
_redis_client = None


def _redis() -> Any:  # redis client typing is loose
    global _redis_client  # process-wide lazy client
    if _redis_client is None:
        import redis

        from app.core.settings import get_settings

        s = get_settings()
        _redis_client = redis.Redis(
            host=s.redis_host, port=s.redis_port, password=s.redis_password or None, db=2,
            decode_responses=True, socket_connect_timeout=1, socket_timeout=2,
        )  # fmt: skip
    return _redis_client


def acquire_kick(tenant_id: uuid.UUID) -> bool:
    """Return True when this caller should enqueue the dispatch (first in the 2 s window); Redis ``SET NX EX``, else in-process."""
    key = f"public_events:kick:{tenant_id}"
    try:
        return bool(_redis().set(key, "1", nx=True, ex=KICK_DEDUP_SECONDS))
    except Exception:
        now = time.monotonic()
        with _local_lock:
            if _local_until.get(key, 0.0) > now:
                return False
            _local_until[key] = now + KICK_DEDUP_SECONDS
            return True


def kick(tenant_id: uuid.UUID) -> None:
    """Installed with ``core.public_events.set_dispatch_kicker``. Eager Celery (tests / dev) dispatches without the
    dedupe window; otherwise the task runs with a 2 s countdown so rows committed inside the window are included."""
    from app.tasks import public_events as tasks
    from app.tasks.runtime import enqueue

    task = tasks.dispatch
    eager = bool(getattr(getattr(getattr(task, "app", None), "conf", None), "task_always_eager", False))
    if eager:
        enqueue(task, str(tenant_id))
        return
    if not acquire_kick(tenant_id):
        return
    try:
        task.apply_async(args=[str(tenant_id)], countdown=KICK_DEDUP_SECONDS)
    except Exception:
        logger.exception("Could not enqueue public_events.dispatch for tenant {}", tenant_id)


__all__ = [
    "KICK_DEDUP_SECONDS",
    "SWEEP_AGE_SECONDS",
    "acquire_kick",
    "dispatch",
    "has_stalled",
    "kick",
    "sweep_tenant",
]
