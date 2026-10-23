# ruff: file-ignore[line-too-long]
"""Receivables (Epic 30) Celery tasks. Every per-tenant task starts with ``tenant_id``.

* ``receivables.apply_credit_note`` / ``receivables.refresh_balance`` (``interactive``): event handlers.
* ``receivables.daily_all`` (beat, daily) -> per tenant: PTP check, overdue scan (``InvoiceOverdue``).
* ``receivables.nightly_all`` (beat, nightly, ``maintenance``) -> per tenant: cache rebuild + reconciliation run.
"""

import uuid

from loguru import logger

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids


def _ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, actor_kind="system")


async def _apply_cn(tenant_id: uuid.UUID, cn_id: uuid.UUID) -> str:
    from app.services.receivables import credits

    async with tenant_session(tenant_id) as db:
        return await credits.apply_credit_note(_ctx(tenant_id), db, cn_id)


@celery_app.task(name="receivables.apply_credit_note", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def apply_credit_note(tenant_id: str, credit_note_id: str) -> str:
    tid = uuid.UUID(str(tenant_id))
    return str(run(lambda: _apply_cn(tid, uuid.UUID(str(credit_note_id))), tenant_id=tid))


async def _refresh(tenant_id: uuid.UUID, doc_id: uuid.UUID) -> None:
    from app.services.receivables import settlement

    async with tenant_session(tenant_id) as db:
        await settlement.refresh_cache(db, [doc_id])


@celery_app.task(name="receivables.refresh_balance", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def refresh_balance(tenant_id: str, document_id: str) -> None:
    tid = uuid.UUID(str(tenant_id))
    run(lambda: _refresh(tid, uuid.UUID(str(document_id))), tenant_id=tid)


async def _daily(tenant_id: uuid.UUID) -> dict[str, int]:
    from app.services.receivables import collections

    async with tenant_session(tenant_id) as db:
        broken = await collections.ptp_check(_ctx(tenant_id), db)
    async with tenant_session(tenant_id) as db:
        overdue = await collections.overdue_scan(_ctx(tenant_id), db)
    return {"ptp_broken": broken, "overdue": overdue}


@celery_app.task(name="receivables.daily", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def daily(tenant_id: str) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))
    result: dict[str, int] = run(lambda: _daily(tid), tenant_id=tid)
    return result


@celery_app.task(name="receivables.daily_all", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def daily_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(daily, str(tid))
    return len(ids)


async def _nightly(tenant_id: uuid.UUID) -> int:
    from app.services.receivables import reconciliation, settlement

    async with tenant_session(tenant_id) as db:
        await settlement.rebuild_cache(db)
    async with tenant_session(tenant_id) as db:
        out = await reconciliation.run(_ctx(tenant_id), db)
    if out.findings_count:
        logger.warning("receivables reconciliation tenant {}: {} findings", tenant_id, out.findings_count)
    return out.findings_count


@celery_app.task(name="receivables.nightly", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def nightly(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _nightly(tid), tenant_id=tid))


@celery_app.task(name="receivables.nightly_all", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def nightly_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(nightly, str(tid))
    return len(ids)
