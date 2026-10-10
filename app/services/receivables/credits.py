# ruff: noqa: E501
"""Client credits and Credit Note application (FR-150, FR-153).

``client_credits`` are signed, append-only rows; the balance is their sum. The ``CreditNoteIssued`` handler is
idempotent (unique ``allocations(credit_note_id, invoice_id)`` for the CN's own application): it allocates the
CN amount to the original Invoice up to its open balance (locked) and writes any excess as
``credit_note_excess``, dated at the CN ``doc_date`` (or today with ``prior_period`` when that month closed).
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.receivables import Allocation, ClientCredit
from app.schemas.receivables import AllocateIn, CreditBalanceOut
from app.services.receivables import allocations as alloc_svc
from app.services.receivables import settlement
from app.services.receivables.common import (
    ZERO,
    assert_open,
    conflict,
    credit_note,
    invalid,
    late_date,
    q2,
    today,
)


async def balance(db: AsyncSession, client_id: uuid.UUID, *, legal_entity_id: uuid.UUID | None = None, currency: str | None = None, as_of: date | None = None) -> Decimal:
    stmt = select(func.coalesce(func.sum(ClientCredit.amount), 0)).where(ClientCredit.client_id == client_id)
    if legal_entity_id is not None:
        stmt = stmt.where(ClientCredit.legal_entity_id == legal_entity_id)
    if currency is not None:
        stmt = stmt.where(ClientCredit.currency == currency)
    if as_of is not None:
        stmt = stmt.where(ClientCredit.credit_date <= as_of)
    return Decimal((await db.execute(stmt)).scalar() or 0)


async def balances_for_client(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID) -> list[CreditBalanceOut]:  # noqa: ARG001
    rows = (
        await db.execute(
            select(ClientCredit.legal_entity_id, ClientCredit.currency, func.sum(ClientCredit.amount))
            .where(ClientCredit.client_id == client_id)
            .group_by(ClientCredit.legal_entity_id, ClientCredit.currency)
        )
    ).all()
    return [CreditBalanceOut(client_id=client_id, legal_entity_id=r[0], currency=r[1], balance=q2(Decimal(r[2]))) for r in rows if Decimal(r[2]) != 0]


async def apply_credit_note(ctx: RequestContext, db: AsyncSession, credit_note_id: uuid.UUID) -> str:
    cn = await credit_note(db, credit_note_id)
    if cn is None or cn.status != "issued" or cn.doc_date is None:
        return "skipped"
    done = (
        await db.execute(
            select(Allocation.id).where(Allocation.credit_note_id == cn.id, Allocation.applied_from_credit.is_(False))
        )
    ).first()
    excess_done = (await db.execute(select(ClientCredit.id).where(ClientCredit.credit_note_id == cn.id, ClientCredit.kind == "credit_note_excess"))).first()
    if done is not None or excess_done is not None:
        return "duplicate"
    on, prior = await late_date(db, cn.legal_entity_id, cn.doc_date)
    bals = await settlement.lock(ctx, db, [("invoice", cn.original_invoice_id)])
    bal = bals[cn.original_invoice_id]
    total = q2(Decimal(cn.document_total))
    take = min(total, bal.open) if bal.doc.status == "issued" or bal.doc.cancel_basis == "full_credit_note" else ZERO
    take = max(take, ZERO)
    if take > 0:
        await alloc_svc.write_allocation(ctx, db, bal, take, on, credit_note=cn, prior_period=prior)
    alloc_svc.credit_row(
        db, legal_entity_id=cn.legal_entity_id, client_id=cn.client_id, currency=cn.currency, on=on,
        kind="credit_note_excess", amount=total - take, credit_note_id=cn.id, prior_period=prior,
    )  # fmt: skip
    await db.flush()
    await settlement.refresh_cache(db, [cn.original_invoice_id])
    return "applied"


async def apply_cn_credit(ctx: RequestContext, db: AsyncSession, credit_note_id: uuid.UUID, data: AllocateIn) -> list[uuid.UUID]:
    """Apply a Credit Note's excess credit to other open documents (allocation + ``applied`` row)."""
    cn = await credit_note(db, credit_note_id)
    if cn is None:
        raise invalid("credit_note_not_found", "Credit note not found")
    available = Decimal((await db.execute(select(func.coalesce(func.sum(ClientCredit.amount), 0)).where(ClientCredit.credit_note_id == cn.id))).scalar() or 0)
    if sum((a.amount for a in data.allocations), ZERO) > available:
        raise invalid("over_allocated", "The allocations exceed the credit note's unapplied amount", available=str(available))
    on = data.allocation_date or await today(db)
    await assert_open(db, cn.legal_entity_id, on)
    bals = await settlement.lock(ctx, db, {(a.document_kind, a.document_id) for a in data.allocations})
    ids = []
    for a in data.allocations:
        bal = bals[a.document_id]
        if bal.doc.currency != cn.currency:
            raise invalid("currency_mismatch", "Currency mismatch")
        if bal.doc.client_id != cn.client_id:
            raise conflict("document_mismatch", "The document belongs to another client")
        row = await alloc_svc.write_allocation(ctx, db, bal, a.amount, on, credit_note=cn, applied_from_credit=True)
        alloc_svc.credit_row(db, legal_entity_id=cn.legal_entity_id, client_id=cn.client_id, currency=cn.currency, on=on, kind="applied", amount=-row.amount, credit_note_id=cn.id, allocation_id=row.id)
        ids.append(row.id)
    await db.flush()
    await settlement.refresh_cache(db, list(bals))
    return ids
