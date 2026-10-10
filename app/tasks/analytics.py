# ruff: noqa: E501, D401
"""Predictive analytics Celery tasks (Epic 48). Queue ``analytics`` (AD-54); the snapshot purge runs on
``maintenance``.

**One tenant per task:** every task except the beat fan-outs takes ``tenant_id`` first and opens exactly one
tenant RLS session (``tenant_session(tenant_id)``) -- no cross-tenant training, pooling or statistic
(``tests/modules/analytics/test_isolation.py`` spies on the session factory and checks every signature here).

* ``analytics.nightly`` (beat 01:00 IST = 19:30 UTC) -> ``analytics.train_tenant(tenant_id)`` per active tenant,
  guarded by the Redis lock ``analytics:train:<tenant>`` (per-tenant concurrency 1). A run that would start after
  05:00 IST is deferred to the next night (unless forced from the admin "Retrain now").
* ``analytics.train_tenant``: every enabled kind sequentially, then ``score_all``.
* ``analytics.score_subject(tenant_id, subject_kind, subject_id)``: the debounced event-driven re-score.
* ``analytics.purge_snapshots`` (beat, ``maintenance``) -> ``analytics.purge_tenant_snapshots(tenant_id)``.
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "analytics"
IST = ZoneInfo("Asia/Kolkata")
WINDOW_END_HOUR_IST = 5
LOCK_TTL_S = 4 * 3600
SUBJECT_KINDS = {
    "demand": ("time_to_fill",),
    "offer": ("offer_drop",),
    "placement": ("offer_drop", "attrition"),
}


def in_window(now: datetime | None = None) -> bool:
    """True between 01:00 and 05:00 IST (the nightly training window)."""
    local = (now or datetime.now(UTC)).astimezone(IST)
    return 1 <= local.hour < WINDOW_END_HOUR_IST


def _lock(tid: uuid.UUID) -> tuple[Any, str] | None:
    """(client, token) when the per-tenant training lock was acquired; (None, "") when Redis is down; None when
    another worker holds it."""
    token = uuid.uuid4().hex
    try:
        from app.services.analytics.hooks import _redis

        client = _redis()
        if not client.set(f"analytics:train:{tid}", token, nx=True, ex=LOCK_TTL_S):
            return None
        return client, token
    except Exception as exc:
        logger.warning("analytics train lock unavailable ({}); continuing", type(exc).__name__)
        return (None, "")


def _unlock(tid: uuid.UUID, held: tuple[Any, str]) -> None:
    client, token = held
    if client is None:
        return
    try:
        key = f"analytics:train:{tid}"
        if client.get(key) == token:
            client.delete(key)
    except Exception:  # pragma: no cover - best effort
        logger.debug("analytics train unlock failed")


async def train_tenant_job(tid: uuid.UUID, now: datetime | None = None) -> dict[str, Any]:
    from app.core import audit
    from app.services.analytics import scoring, training

    async with tenant_session(tid) as db:
        audit.set_actor(db, "system", None)
        trained = await training.train_all(db, tid, now=now)
        await db.commit()
        scored = await scoring.score_all(db)
        return {"trained": trained, "scored": scored}


@celery_app.task(name="analytics.train_tenant", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def train_tenant(tenant_id: str, force: bool = False) -> dict[str, Any] | str:
    tid = uuid.UUID(str(tenant_id))
    if not force and not in_window():
        logger.info("analytics: tenant {} training deferred to the next night (outside 01:00-05:00 IST)", tid)
        return "deferred"
    held = _lock(tid)
    if held is None:
        return "locked"
    try:
        return run(lambda: train_tenant_job(tid), tenant_id=tid)  # type: ignore[no-any-return]
    finally:
        _unlock(tid, held)


@celery_app.task(name="analytics.nightly", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def nightly() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(train_tenant, str(tid))
    return len(ids)


async def score_subject_job(tid: uuid.UUID, subject_kind: str, subject_id: uuid.UUID) -> int:
    from app.core import audit
    from app.services.analytics import scoring

    total = 0
    async with tenant_session(tid) as db:
        audit.set_actor(db, "system", None)
        for kind in SUBJECT_KINDS.get(subject_kind, ()):
            total += await scoring.score(db, kind, [subject_id], subject_kind=subject_kind)
    return total


@celery_app.task(name="analytics.score_subject", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def score_subject(tenant_id: str, subject_kind: str, subject_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(
        run(lambda: score_subject_job(tid, subject_kind, uuid.UUID(str(subject_id))), tenant_id=tid) or 0
    )


async def purge_tenant_job(tid: uuid.UUID) -> int:
    from app.services.analytics import snapshots

    async with tenant_session(tid) as db:
        return await snapshots.purge_expired(db)


@celery_app.task(name="analytics.purge_tenant_snapshots", queue="maintenance", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_tenant_snapshots(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: purge_tenant_job(tid), tenant_id=tid) or 0)


@celery_app.task(name="analytics.purge_snapshots", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_snapshots() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(purge_tenant_snapshots, str(tid))
    return len(ids)


FAN_OUT_TASKS = ("analytics.nightly", "analytics.purge_snapshots")
