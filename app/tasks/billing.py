"""Billing (Epic 29) Celery tasks. Every task starts with ``tenant_id`` (except fan-outs).

* ``billing.build_invoice_run(tenant_id, run_id)`` (queue ``finance_runs``): build an Invoice Run's drafts.
* ``billing.placement_created`` / ``billing.placement_converted`` / ``billing.mark_stale`` (``interactive``).
* ``billing.pending_irn_alerts_all`` (beat, hourly) -> per tenant: documents pending an IRN longer than the
  configured hours and IRP 30-day deadlines within 5 days (log + audit; AD-37 alerts).
* ``billing.integrity_all`` (beat, nightly) -> ``billing.integrity(tenant_id)`` (queue ``maintenance``).
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "finance_runs"


def _ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, actor_kind="system")


async def _build(tenant_id: uuid.UUID, run_id: uuid.UUID) -> str:
    from app.models.billing import InvoiceRun
    from app.services.billing import runs

    try:
        async with tenant_session(tenant_id) as db:
            out = await runs.build_run(_ctx(tenant_id), db, run_id)
            return str(out.status) if out else "missing"
    except Exception as exc:
        logger.exception("invoice run {} failed", run_id)
        async with tenant_session(tenant_id) as db:
            row = (await db.execute(select(InvoiceRun).where(InvoiceRun.id == run_id))).scalar_one_or_none()
            if row is not None:
                row.status = "failed"
                row.error = str(exc)[:2000]
        return "failed"


@celery_app.task(name="billing.build_invoice_run", queue="finance_runs", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def build_invoice_run(tenant_id: str, run_id: str) -> str:
    tid = uuid.UUID(str(tenant_id))
    return str(run(lambda: _build(tid, uuid.UUID(str(run_id))), tenant_id=tid))


async def _placement_created(tenant_id: uuid.UUID, placement_id: uuid.UUID) -> None:
    from app.services.billing import rules

    async with tenant_session(tenant_id) as db:
        await rules.ensure_default_rule(_ctx(tenant_id), db, placement_id)


@celery_app.task(name="billing.placement_created", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def placement_created(tenant_id: str, placement_id: str) -> None:
    tid = uuid.UUID(str(tenant_id))
    run(lambda: _placement_created(tid, uuid.UUID(str(placement_id))), tenant_id=tid)


async def _placement_converted(
    tenant_id: uuid.UUID, placement_id: uuid.UUID, on: date, fee: str | None, currency: str
) -> None:
    from app.services.billing import rules

    if not fee or Decimal(fee) <= 0:
        return
    async with tenant_session(tenant_id) as db:
        await rules.add_schedule_item(
            _ctx(tenant_id), db, placement_id, "conversion_fee", on, Decimal(fee), currency, "Conversion fee"
        )


@celery_app.task(name="billing.placement_converted", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def placement_converted(
    tenant_id: str, placement_id: str, conversion_date: str, fee_amount: str | None, currency: str
) -> None:
    tid = uuid.UUID(str(tenant_id))
    run(
        lambda: _placement_converted(
            tid, uuid.UUID(str(placement_id)), date.fromisoformat(conversion_date), fee_amount, currency
        ),
        tenant_id=tid,
    )


async def _mark_stale(tenant_id: uuid.UUID, placement_id: uuid.UUID) -> int:
    from app.services.billing.hooks import mark_stale as mark

    async with tenant_session(tenant_id) as db:
        return await mark(db, placement_id)


@celery_app.task(name="billing.mark_stale", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def mark_stale(tenant_id: str, placement_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _mark_stale(tid, uuid.UUID(str(placement_id))), tenant_id=tid))


async def _pending_irn(tenant_id: uuid.UUID) -> dict[str, int]:
    from app.core import audit
    from app.services.billing.common import DOC_KINDS, MODELS, now, settings

    out = {"overdue": 0, "deadline": 0}
    async with tenant_session(tenant_id) as db:
        hours = int((await settings(db)).pending_irn_alert_hours)
        for kind in DOC_KINDS:
            model = MODELS[kind][0]
            rows = (
                await db.execute(
                    select(model).where(model.einvoice_status == "pending_irn", model.status == "issued")
                )
            ).scalars()
            for doc in rows:
                if doc.issued_at and now() - doc.issued_at > timedelta(hours=hours):
                    out["overdue"] += 1
                if doc.irn_report_by and doc.irn_report_by - date.today() <= timedelta(days=5):
                    out["deadline"] += 1
        if out["overdue"] or out["deadline"]:
            audit.record("einvoice.pending_alert", "einvoice", None, session=db, **out)
    return out


@celery_app.task(name="billing.pending_irn_alerts", queue="maintenance", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def pending_irn_alerts(tenant_id: str) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _pending_irn(tid), tenant_id=tid))


@celery_app.task(name="billing.pending_irn_alerts_all", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def pending_irn_alerts_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(pending_irn_alerts, str(tid))
    return len(ids)


async def _integrity(tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    from app.services.billing import integrity

    async with tenant_session(tenant_id) as db:
        findings = await integrity.run_all(_ctx(tenant_id), db, date.today())
    for f in findings:
        logger.warning("billing integrity finding {} {} {}", f.check, f.subject_kind, f.subject_id)
    return [{"check": f.check, "subject_id": str(f.subject_id)} for f in findings]


@celery_app.task(name="billing.integrity", queue="maintenance", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def integrity_check(tenant_id: str) -> list[dict[str, Any]]:
    tid = uuid.UUID(str(tenant_id))
    return list(run(lambda: _integrity(tid), tenant_id=tid))


@celery_app.task(name="billing.integrity_all", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def integrity_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(integrity_check, str(tid))
    return len(ids)
