# ruff: file-ignore[line-too-long]
"""Allocation / TDS fact writers with the per-allocation FX rule (AD-20, review M8).

* ``inr_at_payment = quantize(amount x payment.fx_rate)``; the payment's final allocation takes the payment-INR
  remainder.
* ``inr_at_document = quantize(amount x doc.fx_rate)``; the fact that brings a document to zero takes the
  document-INR remainder (``inr_document_total - sum of prior INR``).
* ``realized_fx = inr_at_payment - inr_at_document``.
Reversals copy the original's values negated (never recomputed).
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.receivables import Allocation, AllocationReversal, ClientCredit, Payment, TdsDeduction
from app.services.receivables.common import ZERO, conflict, q2, quarter_of, target_cols, tenant_of, user_id
from app.services.receivables.settlement import Balance


async def payment_totals(db: AsyncSession, payment_id: uuid.UUID) -> tuple[Decimal, Decimal]:
    """(live allocated amount, live allocated INR at payment) of a payment."""
    a = (
        await db.execute(
            select(func.coalesce(func.sum(Allocation.amount), 0), func.coalesce(func.sum(Allocation.inr_at_payment), 0)).where(Allocation.payment_id == payment_id)
        )
    ).one()
    r = (
        await db.execute(
            select(func.coalesce(func.sum(AllocationReversal.amount), 0), func.coalesce(func.sum(AllocationReversal.inr_at_payment), 0))
            .join(Allocation, Allocation.id == AllocationReversal.allocation_id)
            .where(Allocation.payment_id == payment_id)
        )
    ).one()
    return Decimal(a[0]) + Decimal(r[0]), Decimal(a[1]) + Decimal(r[1])


async def payment_credit(db: AsyncSession, payment_id: uuid.UUID) -> Decimal:
    return Decimal((await db.execute(select(func.coalesce(func.sum(ClientCredit.amount), 0)).where(ClientCredit.payment_id == payment_id))).scalar() or 0)


def _doc_inr(bal: Balance, amount: Decimal) -> Decimal:
    if amount == bal.open:
        return q2(bal.doc.inr_document_total - bal.settled_inr)
    return q2(amount * bal.doc.fx)


def _consume(bal: Balance, amount: Decimal, inr: Decimal) -> None:
    bal.open = q2(bal.open - amount)
    bal.settled_inr = q2(bal.settled_inr + inr)


async def write_allocation(
    ctx: RequestContext,
    db: AsyncSession,
    bal: Balance,
    amount: Decimal,
    on: date,
    *,
    payment: Payment | None = None,
    credit_note: Any = None,
    prior_period: bool = False,
    applied_from_credit: bool = False,
) -> Allocation:
    amount = q2(amount)
    if amount <= 0:
        raise conflict("invalid_amount", "Allocation amounts must be positive")
    if amount > bal.open:
        raise conflict(
            "allocation_exceeds_open", "The allocation exceeds the open balance",
            document_id=str(bal.doc.id), open=str(bal.open), amount=str(amount),
        )  # fmt: skip
    inr_doc = _doc_inr(bal, amount)
    if payment is not None:
        allocated, inr_used = await payment_totals(db, payment.id)
        fx = Decimal(1) if payment.currency == "INR" else Decimal(payment.fx_rate or 1)
        final = allocated + amount == payment.amount
        inr_pay = q2(payment.inr_amount - inr_used) if final else q2(amount * fx)
    else:
        fx = Decimal(1) if credit_note is None or credit_note.currency == "INR" or credit_note.fx_rate is None else Decimal(credit_note.fx_rate)
        inr_pay = q2(amount * fx)
    row = Allocation(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        legal_entity_id=bal.doc.legal_entity_id,
        client_id=bal.doc.client_id,
        source_kind="payment" if payment is not None else "credit_note",
        payment_id=payment.id if payment is not None else None,
        credit_note_id=credit_note.id if credit_note is not None else None,
        **target_cols(bal.doc.kind, bal.doc.id),
        allocation_date=on,
        amount=amount,
        currency=bal.doc.currency,
        inr_at_document=inr_doc,
        inr_at_payment=inr_pay,
        realized_fx=q2(inr_pay - inr_doc),
        applied_from_credit=applied_from_credit,
        prior_period=prior_period,
        created_by=user_id(ctx),
    )
    db.add(row)
    await db.flush()
    _consume(bal, amount, inr_doc)
    return row


async def write_tds(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    bal: Balance,
    *,
    kind: str,
    amount: Decimal,
    rate_pct: Decimal,
    expected: Decimal,
    variance_flag: bool,
    on: date,
    payment_id: uuid.UUID | None,
    section_id: uuid.UUID | None,
    section_code: str | None,
    prior_period: bool = False,
) -> TdsDeduction:
    amount = q2(amount)
    if amount > bal.open:
        raise conflict("allocation_exceeds_open", "The TDS exceeds the open balance", document_id=str(bal.doc.id), open=str(bal.open), amount=str(amount))
    inr = _doc_inr(bal, amount)
    row = TdsDeduction(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        legal_entity_id=bal.doc.legal_entity_id,
        client_id=bal.doc.client_id,
        payment_id=payment_id,
        **target_cols(bal.doc.kind, bal.doc.id),
        kind=kind,
        section_id=section_id,
        section_code=section_code,
        rate_pct=rate_pct,
        base_amount=bal.doc.taxable_total,
        expected_amount=expected,
        amount=amount,
        currency=bal.doc.currency,
        inr_amount=inr,
        variance_flag=variance_flag,
        deduction_date=on,
        quarter=quarter_of(on),
        prior_period=prior_period,
    )
    db.add(row)
    await db.flush()
    _consume(bal, amount, inr)
    return row


async def write_reversal(ctx: RequestContext, db: AsyncSession, alloc: Allocation, on: date, reason: str, prior_period: bool) -> AllocationReversal:
    exists = (await db.execute(select(AllocationReversal.id).where(AllocationReversal.allocation_id == alloc.id))).scalar_one_or_none()
    if exists is not None:
        raise conflict("already_reversed", "The allocation is already reversed", allocation_id=str(alloc.id))
    row = AllocationReversal(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        allocation_id=alloc.id,
        legal_entity_id=alloc.legal_entity_id,
        reversal_date=on,
        reason=reason[:500],
        amount=-alloc.amount,
        inr_at_document=-alloc.inr_at_document,
        inr_at_payment=-alloc.inr_at_payment,
        realized_fx=-alloc.realized_fx,
        prior_period=prior_period,
        created_by=user_id(ctx),
    )
    db.add(row)
    await db.flush()
    return row


def credit_row(db: AsyncSession, *, legal_entity_id: uuid.UUID, client_id: uuid.UUID, currency: str, on: date, kind: str, amount: Decimal,
               payment_id: uuid.UUID | None = None, credit_note_id: uuid.UUID | None = None, allocation_id: uuid.UUID | None = None,
               prior_period: bool = False) -> ClientCredit | None:  # fmt: skip
    if q2(amount) == ZERO:
        return None
    row = ClientCredit(
        id=uuid.uuid4(), tenant_id=tenant_of(db), legal_entity_id=legal_entity_id, client_id=client_id, currency=currency,
        credit_date=on, kind=kind, amount=q2(amount), payment_id=payment_id, credit_note_id=credit_note_id,
        allocation_id=allocation_id, prior_period=prior_period,
    )  # fmt: skip
    db.add(row)
    return row


async def live_allocations(db: AsyncSession, *, payment_id: uuid.UUID | None = None, credit_note_id: uuid.UUID | None = None) -> list[Allocation]:
    reversed_ids = select(AllocationReversal.allocation_id)
    stmt = select(Allocation).where(Allocation.id.not_in(reversed_ids))
    if payment_id is not None:
        stmt = stmt.where(Allocation.payment_id == payment_id)
    if credit_note_id is not None:
        stmt = stmt.where(Allocation.credit_note_id == credit_note_id)
    return list((await db.execute(stmt.order_by(Allocation.created_at))).scalars())


async def set_allocation_status(db: AsyncSession, payment: Payment) -> None:
    if payment.reversed_at is not None:
        payment.allocation_status = "reversed"
        return
    allocated, _ = await payment_totals(db, payment.id)
    if allocated <= 0:
        status = "unallocated"
    elif allocated >= payment.amount:
        status = "fully_allocated"
    else:
        status = "partially_allocated"
    if payment.allocation_status != status:
        payment.allocation_status = status
    await db.flush()
