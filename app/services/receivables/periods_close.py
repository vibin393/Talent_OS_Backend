# ruff: file-ignore[line-too-long, invalid-formatter-suppression-comment]
"""Accounting Period close / reopen orchestration (FR-161, AD-22) on top of ``legal_entities/periods.py``.

* Checklist: every ``register_close_check``; ``blocking`` -> severity ``blocker`` (refuses the close, 409
  ``close_blocked``), otherwise ``warning`` (needs ``acknowledged_warnings``, else 422
  ``warnings_not_acknowledged``).
* Close (★, ``period:close``): ``periods.close`` locks the row FOR UPDATE (in-flight postings hold FOR SHARE),
  re-runs the checks and emits ``AccountingPeriodClosed``; the snapshot is stored in ``period_close_events``
  (``reclosed`` after a reopen).
* Reopen (Finance Admin): starts approval subject ``period_reopen``; the final approval callback (human) calls
  ``periods.reopen``.
Registers its own checks ``receivables.unallocated_payments`` and ``receivables.unapplied_bank_rows`` (warnings).
"""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef, actor_from_ctx, human_only_action, is_human
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.legal_entities import AccountingPeriod
from app.models.receivables import (
    Allocation,
    AllocationReversal,
    BankStatementRow,
    ClientCredit,
    Payment,
    PeriodCloseEvent,
    TdsDeduction,
    WriteOff,
)
from app.schemas.receivables import (
    CheckResultOut,
    CloseEventOut,
    CloseIn,
    PeriodOut,
    PeriodReportOut,
    PriorPeriodItem,
    ReopenIn,
)
from app.services.receivables.common import conflict, invalid, tenant_of

SUBJECT = "period_reopen"


async def _period(db: AsyncSession, period_id: uuid.UUID, *, lock: bool = False) -> AccountingPeriod:
    stmt = select(AccountingPeriod).where(AccountingPeriod.id == period_id)
    if lock:
        stmt = stmt.with_for_update()
    p = (await db.execute(stmt)).scalar_one_or_none()
    if p is None:
        raise NotFound("Accounting period not found")
    return p


async def list_periods(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, year: int | None) -> list[PeriodOut]:  # ruff: ignore[unused-function-argument]
    stmt = select(AccountingPeriod).where(AccountingPeriod.legal_entity_id == legal_entity_id)
    if year is not None:
        stmt = stmt.where(AccountingPeriod.year == year)
    periods = list((await db.execute(stmt.order_by(AccountingPeriod.year, AccountingPeriod.month))).scalars())
    pending = set(
        (await db.execute(select(PeriodCloseEvent.accounting_period_id).where(PeriodCloseEvent.action == "reopen_requested", PeriodCloseEvent.request_status == "pending"))).scalars()
    )
    return [PeriodOut(id=p.id, legal_entity_id=p.legal_entity_id, year=p.year, month=p.month, status=p.status, closed_at=p.closed_at, reopened_count=p.reopened_count, pending_reopen=p.id in pending) for p in periods]


async def checklist(ctx: RequestContext, db: AsyncSession, period_id: uuid.UUID) -> list[CheckResultOut]:
    from app.services import legal_entities_service

    p = await _period(db, period_id)
    results = await legal_entities_service.checklist(ctx, db, p.legal_entity_id, p.year, p.month)
    return [CheckResultOut(name=r.name, label=r.label, severity="blocker" if r.blocking else "warning", count=r.count, link_path=r.link_path) for r in results]


def _actor(ctx: RequestContext) -> ActorRef:
    actor = actor_from_ctx(ctx)
    if not is_human(actor):
        raise human_only_action()
    return actor


async def close(ctx: RequestContext, db: AsyncSession, period_id: uuid.UUID, data: CloseIn) -> PeriodOut:
    from app.services import legal_entities_service

    actor = _actor(ctx)
    p = await _period(db, period_id, lock=True)
    if p.status != "open":
        raise conflict("period_state", "The period is not open", status=p.status)
    results = await checklist(ctx, db, period_id)
    blockers = [r for r in results if r.severity == "blocker" and r.count > 0]
    if blockers:
        raise conflict("close_blocked", "Resolve the blocking close checks first", checks=[r.model_dump() for r in blockers])
    missing = [r.name for r in results if r.severity == "warning" and r.count > 0 and r.name not in data.acknowledged_warnings]
    if missing:
        raise invalid("warnings_not_acknowledged", "Acknowledge the close warnings first", checks=missing)
    reclose = p.reopened_count > 0
    await legal_entities_service.close(ctx, db, p.legal_entity_id, p.year, p.month, actor=actor)
    db.add(
        PeriodCloseEvent(
            id=uuid.uuid4(), tenant_id=tenant_of(db), accounting_period_id=p.id, legal_entity_id=p.legal_entity_id, year=p.year, month=p.month,
            action="reclosed" if reclose else "close", actor_kind=actor.kind, actor_id=actor.id,
            checklist_snapshot=[r.model_dump() for r in results], acknowledged_warnings=list(data.acknowledged_warnings),
        )
    )
    await db.flush()
    p = await _period(db, period_id)
    return PeriodOut(id=p.id, legal_entity_id=p.legal_entity_id, year=p.year, month=p.month, status=p.status, closed_at=p.closed_at, reopened_count=p.reopened_count)


async def request_reopen(ctx: RequestContext, db: AsyncSession, period_id: uuid.UUID, data: ReopenIn) -> CloseEventOut:
    from app.services.approvals import service as approvals

    actor = _actor(ctx)
    p = await _period(db, period_id, lock=True)
    if p.status != "closed":
        raise conflict("period_state", "The period is not closed", status=p.status)
    pending = (await db.execute(select(PeriodCloseEvent.id).where(PeriodCloseEvent.accounting_period_id == p.id, PeriodCloseEvent.action == "reopen_requested", PeriodCloseEvent.request_status == "pending"))).first()
    if pending is not None:
        raise conflict("reopen_pending", "A reopen request is already pending")
    event = PeriodCloseEvent(
        id=uuid.uuid4(), tenant_id=tenant_of(db), accounting_period_id=p.id, legal_entity_id=p.legal_entity_id, year=p.year, month=p.month,
        action="reopen_requested", actor_kind=actor.kind, actor_id=actor.id, reason=data.reason, request_status="pending",
    )  # fmt: skip
    db.add(event)
    await db.flush()
    result = await approvals.start(ctx, db, SUBJECT, event.id, {"period": f"{p.year:04d}-{p.month:02d}", "legal_entity_id": str(p.legal_entity_id), "reason": data.reason})
    await db.refresh(event)
    if event.approval_request_id is None and result.request_id is not None:
        event.approval_request_id = result.request_id
        await db.flush()
        await db.refresh(event)
    return CloseEventOut.model_validate(event)


async def on_final_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:
    from app.services import legal_entities_service

    event = (await db.execute(select(PeriodCloseEvent).where(PeriodCloseEvent.id == decision.subject_id).with_for_update())).scalar_one_or_none()
    if event is None or event.request_status != "pending":
        return
    if decision.request_id is not None:
        event.approval_request_id = decision.request_id
    approved = decision.outcome in ("approved", "auto_approved")
    approver = decision.actor if decision.actor is not None and is_human(decision.actor) else ActorRef("user", event.actor_id)
    event.request_status = "approved" if approved else "rejected"
    await db.flush()
    if approved:
        await legal_entities_service.reopen(ctx, db, event.legal_entity_id, event.year, event.month, actor=approver, reason=event.reason or "reopen")
    db.add(
        PeriodCloseEvent(
            id=uuid.uuid4(), tenant_id=event.tenant_id, accounting_period_id=event.accounting_period_id, legal_entity_id=event.legal_entity_id,
            year=event.year, month=event.month, action="reopened" if approved else "reopen_rejected", actor_kind=approver.kind, actor_id=approver.id,
            reason=event.reason, approval_request_id=event.approval_request_id,
        )
    )
    await db.flush()


async def report(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, year: int | None = None) -> PeriodReportOut:  # ruff: ignore[unused-function-argument]
    stmt = select(PeriodCloseEvent).where(PeriodCloseEvent.legal_entity_id == legal_entity_id)
    if year is not None:
        stmt = stmt.where(PeriodCloseEvent.year == year)
    events = [CloseEventOut.model_validate(e) for e in (await db.execute(stmt.order_by(PeriodCloseEvent.at.desc()))).scalars()]
    items: list[PriorPeriodItem] = []
    for kind, model, col, amount in (
        ("payment", Payment, Payment.payment_date, Payment.amount),
        ("allocation", Allocation, Allocation.allocation_date, Allocation.amount),
        ("allocation_reversal", AllocationReversal, AllocationReversal.reversal_date, AllocationReversal.amount),
        ("tds", TdsDeduction, TdsDeduction.deduction_date, TdsDeduction.amount),
        ("write_off", WriteOff, WriteOff.write_off_date, WriteOff.amount),
        ("client_credit", ClientCredit, ClientCredit.credit_date, ClientCredit.amount),
    ):
        q = select(model.id, col, amount, getattr(model, "currency", None) if hasattr(model, "currency") else None).where(model.legal_entity_id == legal_entity_id, model.prior_period.is_(True))
        for rid, d, amt, ccy in (await db.execute(q)).all():
            items.append(PriorPeriodItem(kind=kind, id=rid, date=d, amount=amt, currency=ccy or "INR"))
    return PeriodReportOut(events=events, prior_period_items=items)


# ---------------------------------------------------------------- close checks registered by this domain
async def unallocated_payments_check(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, start: date, end: date) -> Any:  # ruff: ignore[unused-function-argument]
    from app.services.legal_entities.periods import CloseCheckResult

    count = (
        await db.execute(
            select(func.count(Payment.id)).where(
                Payment.legal_entity_id == legal_entity_id, Payment.payment_date >= start, Payment.payment_date <= end,
                Payment.allocation_status.in_(("unallocated", "partially_allocated")),
            )
        )
    ).scalar() or 0
    return CloseCheckResult("receivables.unallocated_payments", "Payments not fully allocated", False, int(count), "/receivables/payments?status=unallocated")


async def unapplied_bank_rows_check(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, start: date, end: date) -> Any:  # ruff: ignore[unused-function-argument]
    from app.services.legal_entities.periods import CloseCheckResult

    count = (
        await db.execute(
            select(func.count(BankStatementRow.id)).where(
                BankStatementRow.legal_entity_id == legal_entity_id, BankStatementRow.date >= start, BankStatementRow.date <= end,
                BankStatementRow.match_kind.in_(("matched", "unmatched")),
            )
        )
    ).scalar() or 0
    return CloseCheckResult("receivables.unapplied_bank_rows", "Bank statement rows not applied", False, int(count), "/receivables/bank-imports")
