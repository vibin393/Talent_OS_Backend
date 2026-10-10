"""Reporting Celery tasks (Epic 11; AD-9). Every task starts with ``tenant_id`` and is idempotent.

* ``reporting.run_export(tenant_id, export_id)`` -- produce a queued (large) export with the requester's
  rebuilt principal; holds one of the tenant's ``EXPORT_ASYNC_CONCURRENCY_PER_TENANT`` slots (NFR-16) and
  retries later when both are taken. Only a ``queued`` export runs (done / running / failed: no-op).
* ``reporting.purge_expired_exports_all`` (beat, hourly) -> ``reporting.purge_expired_exports(tenant_id)``.
* ``reporting.purge_active_days_all`` (beat, daily) -> ``reporting.purge_active_days(tenant_id)``.
"""

import uuid
from typing import Any

from loguru import logger

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "interactive"  # Epic 13 (AD-37): the queue of every task in this module


async def _run_export(tenant_id: uuid.UUID, export_id: uuid.UUID) -> str:
    from app.core import fairness
    from app.core.settings import get_settings
    from app.services import notifications_service, reporting_service

    notifications_service.register()  # the ExportCompleted -> export_ready handler must exist in this process
    reporting_service.register()
    limit = get_settings().export_async_concurrency_per_tenant
    async with fairness.tenant_slot(tenant_id, "export", limit=limit):
        try:
            async with tenant_session(tenant_id) as db:
                return await reporting_service.run_export(db, export_id)
        except Exception:
            logger.exception("Export {} failed unexpectedly", export_id)
            from app.services.reporting import export as export_engine

            async with tenant_session(tenant_id) as db:
                await export_engine.mark_failed(db, export_id, "internal_error")
            return "failed"


@celery_app.task(name="reporting.run_export", acks_late=True, bind=True, max_retries=None)  # type: ignore[misc,untyped-decorator,unused-ignore]
def run_export(self: Any, tenant_id: str, export_id: str) -> str:
    from app.core import fairness

    tid, eid = uuid.UUID(str(tenant_id)), uuid.UUID(str(export_id))
    try:
        return str(run(lambda: _run_export(tid, eid), tenant_id=tid))
    except fairness.SlotUnavailable as exc:
        raise self.retry(countdown=fairness.retry_countdown(self.request.retries), exc=exc) from exc


async def _purge_exports(tenant_id: uuid.UUID) -> int:
    from app.services import reporting_service

    async with tenant_session(tenant_id) as db:
        return await reporting_service.purge_expired_exports(db)


@celery_app.task(name="reporting.purge_expired_exports", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_expired_exports(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _purge_exports(tid), tenant_id=tid))


@celery_app.task(name="reporting.purge_expired_exports_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_expired_exports_all() -> int:
    ids: list[uuid.UUID] = run(lambda: tenant_ids(("active", "suspended", "offboarding")))
    for tid in ids:
        enqueue(purge_expired_exports, str(tid))
    return len(ids)


async def _purge_active_days(tenant_id: uuid.UUID) -> int:
    from app.services import reporting_service

    async with tenant_session(tenant_id) as db:
        return await reporting_service.purge_active_days(db)


@celery_app.task(name="reporting.purge_active_days", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_active_days(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _purge_active_days(tid), tenant_id=tid))


@celery_app.task(name="reporting.purge_active_days_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_active_days_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(purge_active_days, str(tid))
    return len(ids)


# ================================================================ Epic 34: summaries (queue ``maintenance``)
async def _rebuild(tenant_id: uuid.UUID, legal_entity_id: uuid.UUID, month: str) -> int:
    from datetime import date

    from app.services import reporting_service

    async with tenant_session(tenant_id) as db:
        fin, ops = await reporting_service.rebuild_summaries(db, legal_entity_id, date.fromisoformat(month))
    return fin + ops


@celery_app.task(name="reporting.rebuild_summaries", acks_late=True, queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def rebuild_summaries(tenant_id: str, legal_entity_id: str, month: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _rebuild(tid, uuid.UUID(str(legal_entity_id)), month), tenant_id=tid))


async def _claim_debounce(key: str, seconds: int) -> bool:
    """``SET NX EX`` on the debounce key; True = this trigger schedules the rebuild (Redis down -> True)."""
    from app.core.settings import get_settings

    s = get_settings()
    if seconds <= 0 or s.rate_limit_backend != "redis":
        return True
    try:
        from redis.asyncio import Redis

        client = Redis(host=s.redis_host, port=s.redis_port, db=3, password=s.redis_password or None,
                       socket_connect_timeout=0.3, socket_timeout=0.3)  # fmt: skip
        try:
            return bool(await client.set(key, "1", nx=True, ex=seconds))
        finally:
            await client.aclose()
    except Exception:
        return True


@celery_app.task(name="reporting.rebuild_summaries_debounced", acks_late=True, queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def rebuild_summaries_debounced(tenant_id: str, legal_entity_id: str, month: str) -> int:
    """Debounced through ``rpt:rebuild:{tenant}:{le}:{month}`` for ``REPORTING_SUMMARY_DEBOUNCE_SECONDS``."""
    from app.core.settings import get_settings

    seconds = get_settings().reporting_summary_debounce_seconds
    key = f"rpt:rebuild:{tenant_id}:{legal_entity_id}:{month}"
    if not run(lambda: _claim_debounce(key, seconds)):
        return 0
    if seconds > 0 and not get_settings().celery_task_always_eager:
        rebuild_summaries.apply_async((tenant_id, legal_entity_id, month), countdown=seconds)
        return 1
    return int(rebuild_summaries(tenant_id, legal_entity_id, month))


async def _document_key(tenant_id: uuid.UUID, kind: str, doc_id: uuid.UUID) -> tuple[str, str] | None:
    from sqlalchemy import select

    from app.services.reporting import p2tables

    tables = {
        "invoice": p2tables.invoices,
        "credit_note": p2tables.credit_notes,
        "debit_note": p2tables.debit_notes,
        "placement": p2tables.placements,
    }
    tbl = tables[kind]
    async with tenant_session(tenant_id) as db:
        if kind == "placement":
            stmt = select(tbl.c.legal_entity_id, tbl.c.end_date, tbl.c.actual_start_date).where(
                tbl.c.id == doc_id
            )
            row = (await db.execute(stmt)).first()
            if row is None:
                return None
            day = row[1] or row[2]
            return (str(row[0]), day.replace(day=1).isoformat()) if day else None
        row = (
            await db.execute(select(tbl.c.legal_entity_id, tbl.c.doc_date).where(tbl.c.id == doc_id))
        ).first()
        return (str(row[0]), row[1].replace(day=1).isoformat()) if row and row[1] else None


@celery_app.task(name="reporting.rebuild_for_document", acks_late=True, queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def rebuild_for_document(tenant_id: str, kind: str, doc_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    key = run(lambda: _document_key(tid, kind, uuid.UUID(str(doc_id))), tenant_id=tid)
    if key is None:
        return 0
    return int(rebuild_summaries_debounced(tenant_id, key[0], key[1]))


async def _nightly(tenant_id: uuid.UUID) -> int:
    from app.services.reporting import summaries

    async with tenant_session(tenant_id) as db:
        return await summaries.rebuild_recent(db, 3)


@celery_app.task(name="reporting.nightly_rebuild", acks_late=True, queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def nightly_rebuild(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _nightly(tid), tenant_id=tid))


async def _verify(tenant_id: uuid.UUID) -> dict[str, int]:
    from app.services import reporting_service

    async with tenant_session(tenant_id) as db:
        return await reporting_service.verify_summaries(db)


@celery_app.task(name="reporting.verify_summaries", acks_late=True, queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def verify_summaries(tenant_id: str) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _verify(tid), tenant_id=tid))


@celery_app.task(name="reporting.nightly_rebuild_all", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def nightly_rebuild_all() -> int:
    ids: list[uuid.UUID] = run(lambda: tenant_ids(("active",)))
    for tid in ids:
        enqueue(nightly_rebuild, str(tid))
    return len(ids)


@celery_app.task(name="reporting.verify_summaries_all", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def verify_summaries_all() -> int:
    ids: list[uuid.UUID] = run(lambda: tenant_ids(("active",)))
    for tid in ids:
        enqueue(verify_summaries, str(tid))
    return len(ids)
