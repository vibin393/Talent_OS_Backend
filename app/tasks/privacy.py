"""Privacy Celery tasks (Epic 12; AD-9: tenant-scoped tasks take ``tenant_id`` first, idempotent).

* ``purge_ai_derived(tenant_id, candidate_id)`` -- consent withdrawn: delete AI-derived rows.
* ``consent_withdrawal_sweep()`` -- every 15 min: re-purge withdrawn candidates that still have derived rows.
* ``recompute_ai_consent_state(tenant_id)`` -- after an AI-processing basis change.
* ``run_erasure(tenant_id, run_id)`` / ``build_subject_export(tenant_id, dsr_id)`` -- DSR execution.
* ``dsr_due_scan()`` -- daily due-soon / overdue alerts.
* ``retention_tick()`` -- hourly: monthly preview on the 1st at 02:00 tenant time; auto-execution after 7 days.
* ``execute_retention_run(tenant_id, run_id)``.

Importing this module also installs the privacy consent checker in the worker (the gateway re-checks consent
when a queued AI job executes).
"""

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.celery import celery_app
from app.core.database import platform_session, tenant_session
from app.services import privacy
from app.tasks.ops_runtime import for_each_tenant, run

QUEUE = "maintenance"  # Epic 13 (AD-37): the queue of every task in this module

privacy.install()


@celery_app.task(name="privacy.purge_ai_derived", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def purge_ai_derived(tenant_id: str, candidate_id: str) -> dict[str, int]:
    tid, cid = uuid.UUID(tenant_id), uuid.UUID(candidate_id)

    async def job() -> dict[str, int]:
        async with tenant_session(tid) as db:
            return await privacy.consent.purge_ai_derived(db, cid)

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="privacy.consent_withdrawal_sweep")  # type: ignore[untyped-decorator,unused-ignore]
def consent_withdrawal_sweep(tenant_id: str | None = None) -> dict[str, object]:
    async def one(tid: uuid.UUID) -> int:
        async with tenant_session(tid) as db:
            ids = await privacy.consent.withdrawn_with_derived(db)
            for cid in ids:
                await privacy.consent.purge_ai_derived(db, cid)
            return len(ids)

    return run(lambda: for_each_tenant(one, only=tenant_id))  # type: ignore[no-any-return]


@celery_app.task(name="privacy.recompute_ai_consent_state", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def recompute_ai_consent_state(tenant_id: str) -> int:
    tid = uuid.UUID(tenant_id)

    async def job() -> int:
        async with tenant_session(tid) as db:
            return await privacy.recompute_ai_consent_state(db)

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="privacy.run_erasure", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def run_erasure(tenant_id: str, run_id: str) -> str:
    tid, rid = uuid.UUID(tenant_id), uuid.UUID(run_id)
    return run(lambda: privacy.erasure.run_erasure(tid, rid), tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="privacy.build_subject_export", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def build_subject_export(tenant_id: str, dsr_id: str) -> str:
    tid, did = uuid.UUID(tenant_id), uuid.UUID(dsr_id)
    return run(lambda: privacy.export.build_subject_export(tid, did), tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="privacy.dsr_due_scan")  # type: ignore[untyped-decorator,unused-ignore]
def dsr_due_scan(tenant_id: str | None = None) -> dict[str, object]:
    async def one(tid: uuid.UUID) -> dict[str, int]:
        async with tenant_session(tid) as db:
            return await privacy.dsr.due_scan(db, tid)

    return run(lambda: for_each_tenant(one, only=tenant_id))  # type: ignore[no-any-return]


@celery_app.task(name="privacy.execute_retention_run", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def execute_retention_run(tenant_id: str, run_id: str) -> dict[str, int]:
    tid, rid = uuid.UUID(tenant_id), uuid.UUID(run_id)
    return run(lambda: privacy.retention.process_run(tid, rid), tenant_id=tid)  # type: ignore[no-any-return]


async def _retention_tick(now: datetime | None = None, only: str | None = None) -> dict[str, object]:
    from app.models.privacy import RetentionRun
    from app.services import identity_bridge

    at = now or datetime.now(UTC)

    async def one(tid: uuid.UUID) -> str:
        async with platform_session() as pdb:
            tenant = await identity_bridge.get_tenant(pdb, tid)
            tz = ZoneInfo(tenant.timezone) if tenant is not None else UTC
        local = at.astimezone(tz)
        outcome = "idle"
        if local.day == 1 and local.hour == 2:
            month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
            async with tenant_session(tid) as db:
                already = (
                    await db.execute(
                        select(RetentionRun.id).where(
                            RetentionRun.created_by.is_(None), RetentionRun.created_at >= month_start
                        )
                    )
                ).first()
            if already is None:
                outcome = await privacy.retention.monthly(tid)
        started = await privacy.retention.execute_due_auto_runs(tid)
        return f"{outcome}; auto-executed {started}"

    return await for_each_tenant(one, only=only)


@celery_app.task(name="privacy.retention_tick")  # type: ignore[untyped-decorator,unused-ignore]
def retention_tick(tenant_id: str | None = None) -> dict[str, object]:
    return run(lambda: _retention_tick(only=tenant_id))  # type: ignore[no-any-return]


# ================================================================ Epic 35: retention-class expiry (AD-34)
@celery_app.task(name="privacy.retention_expiry_scan")  # type: ignore[untyped-decorator,unused-ignore]
def retention_expiry_scan(tenant_id: str | None = None) -> dict[str, object]:
    """Daily 03:00 IST: preview expired rows per retention class; auto-execute where the class allows it."""

    async def one(tid: uuid.UUID) -> dict[str, int]:
        return await privacy.class_expiry.scan_tenant(tid)

    return run(lambda: for_each_tenant(one, only=tenant_id))  # type: ignore[no-any-return]


@celery_app.task(name="privacy.record_engagement_consent", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def record_engagement_consent(tenant_id: str, candidate_id: str) -> str:
    """``PlacementCreated`` (Epic 35, FR-179): record the ``engagement`` basis (idempotent)."""
    tid, cid = uuid.UUID(tenant_id), uuid.UUID(candidate_id)

    async def job() -> str:
        from app.core import audit

        async with tenant_session(tid) as db:
            audit.set_actor(db, "system", None)
            row = await privacy.consent.record_engagement(db, cid)
            return str(row.id)

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]
