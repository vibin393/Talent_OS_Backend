# ruff: noqa: E501
"""Reconciliation runner (NFR-25): runs every ``register_reconciliation_check`` (core registry) for a tenant, stores
``reconciliation_runs`` / ``reconciliation_findings`` and raises an AD-37 alert (log + audit) on findings.

Checks registered here (``receivables.*``):
* allocations net of reversals <= payment amount and <= document total;
* sum of ``inr_at_payment`` = payment INR for fully allocated payments;
* open balance >= 0 for every document;
* ``invoice_balances`` equals a rebuild from facts;
* payment accounting: allocations + credits of a live payment = payment amount (0 once reversed);
* statement invariant per client / currency;
* every live CN allocation references an issued CN.
"""

import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.reconciliation import Finding, reconciliation_checks
from app.models.billing import CreditNote
from app.models.receivables import (
    Allocation,
    AllocationReversal,
    ClientCredit,
    InvoiceBalance,
    Payment,
    ReconciliationFinding,
    ReconciliationRun,
)
from app.schemas.receivables import ReconFindingOut, ReconRunOut
from app.services.receivables import allocations as alloc_svc
from app.services.receivables import settlement
from app.services.receivables.common import ZERO, documents, now, q2, tenant_of


def _f(check: str, kind: str, sid: uuid.UUID | None, **details: object) -> Finding:
    return Finding(check=check, severity="error", subject_kind=kind, subject_id=sid, details={k: str(v) for k, v in details.items()})


async def check_payments(ctx: RequestContext, db: AsyncSession, as_of: date) -> list[Finding]:  # noqa: ARG001
    out = []
    for p in (await db.execute(select(Payment))).scalars():
        allocated, inr = await alloc_svc.payment_totals(db, p.id)
        credit = await alloc_svc.payment_credit(db, p.id)
        if allocated > p.amount:
            out.append(_f("receivables.payment_over_allocated", "payment", p.id, allocated=allocated, amount=p.amount))
        if p.reversed_at is None and p.allocation_status == "fully_allocated" and q2(inr) != q2(p.inr_amount):
            out.append(_f("receivables.payment_inr_mismatch", "payment", p.id, allocated_inr=inr, payment_inr=p.inr_amount))
        expected = ZERO if p.reversed_at is not None else p.amount
        if q2(allocated + credit) != q2(expected):
            out.append(_f("receivables.payment_unbalanced", "payment", p.id, allocated=allocated, credit=credit, expected=expected))
    return out


async def check_documents(ctx: RequestContext, db: AsyncSession, as_of: date) -> list[Finding]:  # noqa: ARG001
    out = []
    docs = await documents(db)
    bals = await settlement.balances(db, docs)
    cache = {c.receivable_id: c for c in (await db.execute(select(InvoiceBalance))).scalars()}
    for b in bals.values():
        if b.open < 0:
            out.append(_f("receivables.open_negative", b.doc.kind, b.doc.id, open=b.open))
        c = cache.get(b.doc.id)
        if c is not None and q2(c.open_amount) != b.open:
            out.append(_f("receivables.cache_mismatch", b.doc.kind, b.doc.id, cache=c.open_amount, facts=b.open))
    return out


async def check_credit_notes(ctx: RequestContext, db: AsyncSession, as_of: date) -> list[Finding]:  # noqa: ARG001
    reversed_ids = select(AllocationReversal.allocation_id)
    rows = (
        await db.execute(
            select(Allocation.id, Allocation.credit_note_id, CreditNote.status)
            .outerjoin(CreditNote, CreditNote.id == Allocation.credit_note_id)
            .where(Allocation.source_kind == "credit_note", Allocation.id.not_in(reversed_ids))
        )
    ).all()
    return [_f("receivables.cn_allocation_unissued", "allocation", r[0], credit_note_id=r[1], status=r[2]) for r in rows if r[2] != "issued"]


async def check_statements(ctx: RequestContext, db: AsyncSession, as_of: date) -> list[Finding]:
    from app.services.receivables import statements

    pairs: set[tuple[uuid.UUID, str]] = {(d.client_id, d.currency) for d in await documents(db)}
    pairs |= {(r[0], r[1]) for r in (await db.execute(select(ClientCredit.client_id, ClientCredit.currency).distinct())).all()}
    out = []
    for client_id, currency in pairs:
        try:
            await statements.build(ctx, db, client_id, currency, date(2000, 1, 1), as_of)
        except statements.StatementMismatch as exc:
            out.append(_f("receivables.statement_invariant", "client", client_id, error=str(exc)[:300]))
    return out


CHECKS = {
    "receivables.payments": check_payments,
    "receivables.documents": check_documents,
    "receivables.credit_notes": check_credit_notes,
    "receivables.statements": check_statements,
}


async def run(ctx: RequestContext, db: AsyncSession, as_of: date | None = None) -> ReconRunOut:
    from app.services.receivables.common import today

    as_of = as_of or await today(db)
    run_row = ReconciliationRun(id=uuid.uuid4(), tenant_id=tenant_of(db), as_of=as_of, status="running")
    db.add(run_row)
    await db.flush()
    findings: list[Finding] = []
    failures: dict[str, str] = defaultdict(str)
    for name, fn in sorted(reconciliation_checks().items()):
        try:
            async with db.begin_nested():
                findings.extend(await fn(ctx, db, as_of))
        except Exception as exc:
            logger.exception("reconciliation check {} failed", name)
            failures[name] = str(exc)[:200]
    for f in findings:
        db.add(ReconciliationFinding(id=uuid.uuid4(), tenant_id=run_row.tenant_id, run_id=run_row.id, check=f.check[:80], severity=f.severity, subject_kind=f.subject_kind[:40], subject_id=f.subject_id, details=dict(f.details)))
    run_row.findings_count = len(findings)
    run_row.status = "completed" if not failures else "failed"
    run_row.error = "; ".join(f"{k}: {v}" for k, v in failures.items())[:500] or None
    run_row.finished_at = now()
    await db.flush()
    if findings or failures:
        logger.warning("ALERT reconciliation: tenant {} run {} has {} findings", run_row.tenant_id, run_row.id, len(findings))
        audit.record("reconciliation.findings", "reconciliation_run", run_row.id, session=db, findings=len(findings), failed_checks=len(failures))
    await db.refresh(run_row)
    return ReconRunOut.model_validate(run_row)


async def list_runs(ctx: RequestContext, db: AsyncSession) -> list[ReconRunOut]:  # noqa: ARG001
    return [ReconRunOut.model_validate(r) for r in (await db.execute(select(ReconciliationRun).order_by(ReconciliationRun.started_at.desc()).limit(100))).scalars()]


async def findings(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> list[ReconFindingOut]:  # noqa: ARG001
    rows = (await db.execute(select(ReconciliationFinding).where(ReconciliationFinding.run_id == run_id).order_by(ReconciliationFinding.check))).scalars()
    return [ReconFindingOut.model_validate(r) for r in rows]


_ = (Decimal, func)
