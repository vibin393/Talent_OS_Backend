# ruff: noqa: E501, RUF028
"""Payments, allocations and reversals (FR-153, §5.13).

Record: period open on ``payment_date`` -> lock the targets (``lock_for_settlement``, uuid order) -> TDS facts ->
allocations (manual list or auto: oldest due first, non-disputed open, same Legal Entity / client / currency) ->
remainder as a ``payment_remainder`` client credit -> cache refresh -> ``PaymentRecorded``.

Reversal of a payment (human-only, ★) reverses every live allocation (and optionally its TDS), negates the
payment's remaining credit and marks it ``reversed``; everything is dated in an open period, ``prior_period``
when the original month is closed.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, sequences
from app.core.actors import human_only_action, is_human
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.events.receivables import PaymentRecorded, PaymentReversed
from app.models.receivables import (
    Allocation,
    AllocationReversal,
    Payment,
    TdsDeduction,
    TdsDeductionReversal,
    TdsSection,
)
from app.schemas.receivables import (
    AllocateIn,
    AllocationIn,
    AllocationOut,
    OpenDocumentOut,
    PaymentDetailOut,
    PaymentIn,
    PaymentOut,
    PaymentReverseIn,
    ReverseIn,
    TdsIn,
    TdsOut,
)
from app.services.receivables import allocations as alloc_svc
from app.services.receivables import settlement
from app.services.receivables import tds as tds_math
from app.services.receivables.common import (
    ZERO,
    assert_open,
    conflict,
    documents,
    invalid,
    late_date,
    now,
    q2,
    target_of,
    tenant_of,
    today,
    user_id,
)
from app.services.receivables.settings import get_settings_row


def _actor_check(ctx: RequestContext) -> None:
    from app.core.actors import actor_from_ctx

    if not is_human(actor_from_ctx(ctx)):
        raise human_only_action()


async def _assert_party(db: AsyncSession, legal_entity_id: uuid.UUID, client_id: uuid.UUID) -> None:
    le = (await db.execute(text("SELECT 1 FROM legal_entities WHERE id = :i"), {"i": legal_entity_id})).first()
    cl = (await db.execute(text("SELECT 1 FROM clients WHERE id = :i"), {"i": client_id})).first()
    if le is None or cl is None:
        raise NotFound("Legal entity or client not found")


def _check_doc(bal: settlement.Balance, legal_entity_id: uuid.UUID, client_id: uuid.UUID, currency: str) -> None:
    doc = bal.doc
    if doc.currency != currency:
        raise invalid("currency_mismatch", "The payment currency differs from the document currency", document_id=str(doc.id), document_currency=doc.currency, payment_currency=currency)
    if doc.legal_entity_id != legal_entity_id or doc.client_id != client_id:
        raise invalid("document_mismatch", "The document belongs to another client or legal entity", document_id=str(doc.id))
    if doc.status != "issued":
        raise conflict("document_not_open", "The document is not open for settlement", document_id=str(doc.id), status=doc.status)


async def _fx(ctx: RequestContext, db: AsyncSession, currency: str, on: date, given: Decimal | None) -> tuple[Decimal | None, str | None]:
    if currency == "INR":
        return None, None
    if given is not None:
        return given, "manual"
    from app.services import legal_entities_service

    rate = await legal_entities_service.rate_for(ctx, db, currency, "INR", on)
    return Decimal(rate.rate), str(rate.source)[:60]


async def _write_tds_list(ctx: RequestContext, db: AsyncSession, items: list[TdsIn], bals: dict[uuid.UUID, settlement.Balance], on: date, payment_id: uuid.UUID | None, prior: bool) -> list[TdsDeduction]:
    settings = await get_settings_row(db)
    out = []
    for item in items:
        bal = bals[item.document_id]
        section = None
        if item.section_id is not None:
            section = (await db.execute(select(TdsSection).where(TdsSection.id == item.section_id))).scalar_one_or_none()
            if section is None:
                raise NotFound("TDS section not found")
        rate = item.rate_pct if item.rate_pct is not None else (section.default_rate_pct if section is not None else None)
        if rate is None:
            raise invalid("tds_rate_required", "A TDS rate or section is required", document_id=str(item.document_id))
        expected = tds_math.expected_tds(bal.doc.taxable_total, Decimal(rate))
        out.append(
            await alloc_svc.write_tds(
                ctx, db, bal, kind=item.kind, amount=item.amount, rate_pct=Decimal(rate), expected=expected,
                variance_flag=tds_math.variance(expected, item.amount, settings.tds_variance_tolerance), on=on,
                payment_id=payment_id, section_id=section.id if section else None, section_code=section.code if section else None,
                prior_period=prior,
            )  # fmt: skip
        )
    return out


async def _auto_targets(db: AsyncSession, legal_entity_id: uuid.UUID, client_id: uuid.UUID, currency: str) -> list[tuple[str, uuid.UUID]]:
    docs = [d for d in await documents(db, legal_entity_id=legal_entity_id, client_id=client_id, currency=currency) if d.status == "issued"]
    bals = await settlement.balances(db, docs)
    return [(b.doc.kind, b.doc.id) for b in bals.values() if b.open - b.disputed > 0]


async def record(ctx: RequestContext, db: AsyncSession, data: PaymentIn, *, import_batch_id: uuid.UUID | None = None) -> PaymentDetailOut:
    await _assert_party(db, data.legal_entity_id, data.client_id)
    await assert_open(db, data.legal_entity_id, data.payment_date)
    fx_rate, fx_source = await _fx(ctx, db, data.currency, data.payment_date, data.fx_rate)
    amount = q2(data.amount)
    inr = amount if fx_rate is None else q2(amount * fx_rate)
    payment = Payment(
        id=uuid.uuid4(), tenant_id=tenant_of(db), human_id=await sequences.next_human_id(db, "payment", "PAY", 6),
        legal_entity_id=data.legal_entity_id, client_id=data.client_id, payment_date=data.payment_date, amount=amount,
        currency=data.currency, fx_rate=fx_rate, fx_source=fx_source, inr_amount=inr, mode=data.mode, reference=data.reference,
        receiving_bank_account_id=data.receiving_bank_account_id, allocation_status="unallocated",
        import_batch_id=import_batch_id, notes=data.notes, created_by=user_id(ctx),
    )  # fmt: skip
    db.add(payment)
    await db.flush()

    manual: list[AllocationIn] = data.allocations if isinstance(data.allocations, list) else []
    keys = {(a.document_kind, a.document_id) for a in manual} | {(t.document_kind, t.document_id) for t in data.tds}
    if data.allocations == "auto":
        keys |= set(await _auto_targets(db, data.legal_entity_id, data.client_id, data.currency))
    bals = await settlement.lock(ctx, db, keys)
    for key in keys:
        _check_doc(bals[key[1]], data.legal_entity_id, data.client_id, data.currency)

    await _write_tds_list(ctx, db, data.tds, bals, data.payment_date, payment.id, False)
    remaining = amount
    plan: list[tuple[settlement.Balance, Decimal]] = []
    if manual:
        plan = [(bals[a.document_id], q2(a.amount)) for a in manual]
    elif data.allocations == "auto":
        ordered = sorted(bals.values(), key=lambda b: (b.doc.due_date or date.max, b.doc.doc_date or date.max, str(b.doc.id)))
        for b in ordered:
            take = min(b.open - b.disputed, remaining)
            if take > 0:
                plan.append((b, q2(take)))
                remaining -= take
        remaining = amount
    if sum((p[1] for p in plan), ZERO) > amount:
        raise invalid("over_allocated", "The allocations exceed the payment amount", amount=str(amount))
    for bal, amt in plan:
        await alloc_svc.write_allocation(ctx, db, bal, amt, data.payment_date, payment=payment)
        remaining -= amt
    alloc_svc.credit_row(
        db, legal_entity_id=payment.legal_entity_id, client_id=payment.client_id, currency=payment.currency,
        on=payment.payment_date, kind="payment_remainder", amount=remaining, payment_id=payment.id,
    )  # fmt: skip
    await db.flush()
    await alloc_svc.set_allocation_status(db, payment)
    await settlement.refresh_cache(db, [k[1] for k in keys])
    audit.record("payment.recorded", "payment", payment.id, session=db, human_id=payment.human_id, amount=str(amount), currency=payment.currency)
    emit(db, PaymentRecorded(tenant_id=payment.tenant_id, payment_id=payment.id, client_id=payment.client_id, legal_entity_id=payment.legal_entity_id))
    return await get_payment(ctx, db, payment.id)


async def _payment(db: AsyncSession, payment_id: uuid.UUID, *, lock: bool = False) -> Payment:
    stmt = select(Payment).where(Payment.id == payment_id)
    if lock:
        stmt = stmt.with_for_update()
    p = (await db.execute(stmt)).scalar_one_or_none()
    if p is None:
        raise NotFound("Payment not found")
    return p


async def allocate(ctx: RequestContext, db: AsyncSession, payment_id: uuid.UUID, data: AllocateIn) -> PaymentDetailOut:
    """Apply a payment's unallocated credit to documents (allocation + ``applied`` credit row)."""
    payment = await _payment(db, payment_id, lock=True)
    if payment.reversed_at is not None:
        raise conflict("already_reversed", "The payment is reversed")
    on = data.allocation_date or await today(db)
    await assert_open(db, payment.legal_entity_id, on)
    available = await alloc_svc.payment_credit(db, payment.id)
    total = sum((q2(a.amount) for a in data.allocations), ZERO)
    if total > available:
        raise invalid("over_allocated", "The allocations exceed the unallocated amount", available=str(available))
    bals = await settlement.lock(ctx, db, {(a.document_kind, a.document_id) for a in data.allocations})
    for a in data.allocations:
        bal = bals[a.document_id]
        _check_doc(bal, payment.legal_entity_id, payment.client_id, payment.currency)
        row = await alloc_svc.write_allocation(ctx, db, bal, a.amount, on, payment=payment, applied_from_credit=True)
        alloc_svc.credit_row(
            db, legal_entity_id=payment.legal_entity_id, client_id=payment.client_id, currency=payment.currency, on=on,
            kind="applied", amount=-row.amount, payment_id=payment.id, allocation_id=row.id,
        )  # fmt: skip
    await db.flush()
    await alloc_svc.set_allocation_status(db, payment)
    await settlement.refresh_cache(db, list(bals))
    return await get_payment(ctx, db, payment.id)


async def reverse_allocation(ctx: RequestContext, db: AsyncSession, allocation_id: uuid.UUID, data: ReverseIn) -> AllocationOut:
    alloc = (await db.execute(select(Allocation).where(Allocation.id == allocation_id))).scalar_one_or_none()
    if alloc is None:
        raise NotFound("Allocation not found")
    payment = await _payment(db, alloc.payment_id, lock=True) if alloc.payment_id else None
    kind, doc_id = target_of(alloc)
    await settlement.lock(ctx, db, [(kind, doc_id)])
    on, prior = await _reversal_date(db, alloc.legal_entity_id, data.reversal_date, alloc.allocation_date)
    rev = await alloc_svc.write_reversal(ctx, db, alloc, on, data.reason, prior)
    alloc_svc.credit_row(
        db, legal_entity_id=alloc.legal_entity_id, client_id=alloc.client_id, currency=alloc.currency, on=on, kind="reversal",
        amount=alloc.amount, payment_id=alloc.payment_id, credit_note_id=alloc.credit_note_id, allocation_id=alloc.id, prior_period=prior,
    )  # fmt: skip
    await db.flush()
    if payment is not None:
        await alloc_svc.set_allocation_status(db, payment)
    await settlement.refresh_cache(db, [doc_id])
    audit.record("allocation.reversed", "allocation", alloc.id, session=db, reason=data.reason[:200])
    return _alloc_out(alloc, None, rev)


async def _reversal_date(db: AsyncSession, legal_entity_id: uuid.UUID, wanted: date | None, original: date) -> tuple[date, bool]:
    on = wanted or await today(db)
    await assert_open(db, legal_entity_id, on)
    from app.services.receivables.common import is_closed

    return on, await is_closed(db, legal_entity_id, original)


async def reverse(ctx: RequestContext, db: AsyncSession, payment_id: uuid.UUID, data: PaymentReverseIn) -> PaymentDetailOut:
    """★ human-only: bounced cheque / wrong posting."""
    _actor_check(ctx)
    payment = await _payment(db, payment_id, lock=True)
    if payment.reversed_at is not None:
        raise conflict("already_reversed", "The payment is already reversed")
    on, prior = await _reversal_date(db, payment.legal_entity_id, data.reversal_date, payment.payment_date)
    live = await alloc_svc.live_allocations(db, payment_id=payment.id)
    tds_rows: list[TdsDeduction] = []
    if data.reverse_tds:
        reversed_tds = select(TdsDeductionReversal.tds_deduction_id)
        tds_rows = list((await db.execute(select(TdsDeduction).where(TdsDeduction.payment_id == payment.id, TdsDeduction.id.not_in(reversed_tds)))).scalars())
    keys = {target_of(a) for a in live} | {target_of(t) for t in tds_rows}
    await settlement.lock(ctx, db, keys)
    for a in live:
        await alloc_svc.write_reversal(ctx, db, a, on, data.reason, prior)
    for t in tds_rows:
        db.add(TdsDeductionReversal(id=uuid.uuid4(), tenant_id=payment.tenant_id, tds_deduction_id=t.id, legal_entity_id=t.legal_entity_id, reversal_date=on, reason=data.reason[:500], prior_period=prior))
    await db.flush()
    balance = await alloc_svc.payment_credit(db, payment.id)
    alloc_svc.credit_row(
        db, legal_entity_id=payment.legal_entity_id, client_id=payment.client_id, currency=payment.currency, on=on,
        kind="reversal", amount=-balance, payment_id=payment.id, prior_period=prior,
    )  # fmt: skip
    payment.reversed_at = now()
    payment.reversal_reason = data.reason[:500]
    payment.reversal_date = on
    payment.allocation_status = "reversed"
    await db.flush()
    await settlement.refresh_cache(db, [k[1] for k in keys])
    audit.record("payment.reversed", "payment", payment.id, session=db, reason=data.reason[:200], reversal_date=on.isoformat())
    emit(db, PaymentReversed(tenant_id=payment.tenant_id, payment_id=payment.id, reason=data.reason[:500]))
    return await get_payment(ctx, db, payment.id)


# ---------------------------------------------------------------- reads
def _alloc_out(a: Allocation, number: str | None, rev: AllocationReversal | None) -> AllocationOut:
    kind, doc_id = target_of(a)
    return AllocationOut(
        id=a.id, source_kind=a.source_kind, payment_id=a.payment_id, credit_note_id=a.credit_note_id, document_kind=kind,
        document_id=doc_id, document_number=number, allocation_date=a.allocation_date, amount=a.amount, currency=a.currency,
        inr_at_document=a.inr_at_document, inr_at_payment=a.inr_at_payment, realized_fx=a.realized_fx, prior_period=a.prior_period,
        reversed=rev is not None, reversal_date=rev.reversal_date if rev else None,
    )  # fmt: skip


async def _numbers(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str | None]:
    if not ids:
        return {}
    return {d.id: d.number for d in await documents(db, ids=list(ids))}


async def allocations_for(db: AsyncSession, *, payment_id: uuid.UUID | None = None, document_id: uuid.UUID | None = None) -> list[AllocationOut]:
    stmt = select(Allocation, AllocationReversal).outerjoin(AllocationReversal, AllocationReversal.allocation_id == Allocation.id)
    if payment_id is not None:
        stmt = stmt.where(Allocation.payment_id == payment_id)
    if document_id is not None:
        stmt = stmt.where((Allocation.invoice_id == document_id) | (Allocation.debit_note_id == document_id))
    rows = (await db.execute(stmt.order_by(Allocation.allocation_date, Allocation.created_at))).all()
    nums = await _numbers(db, {target_of(a)[1] for a, _ in rows})
    return [_alloc_out(a, nums.get(target_of(a)[1]), r) for a, r in rows]


def tds_out(t: TdsDeduction, number: str | None = None, reversed_: bool = False) -> TdsOut:
    kind, doc_id = target_of(t)
    return TdsOut(
        id=t.id, legal_entity_id=t.legal_entity_id, client_id=t.client_id, payment_id=t.payment_id, document_kind=kind, document_id=doc_id,
        document_number=number, kind=t.kind, section_code=t.section_code, rate_pct=t.rate_pct, base_amount=t.base_amount,
        expected_amount=t.expected_amount, amount=t.amount, currency=t.currency, variance_flag=t.variance_flag,
        deduction_date=t.deduction_date, quarter=t.quarter, certificate_status=t.certificate_status, certificate_ref=t.certificate_ref,
        certificate_received_at=t.certificate_received_at, reversed=reversed_,
    )  # fmt: skip


async def _payment_out(db: AsyncSession, p: Payment) -> dict[str, Any]:
    out = PaymentOut.model_validate(p).model_dump()
    out["unallocated"] = await alloc_svc.payment_credit(db, p.id)
    return out


async def get_payment(ctx: RequestContext, db: AsyncSession, payment_id: uuid.UUID) -> PaymentDetailOut:  # noqa: ARG001
    p = await _payment(db, payment_id)
    tds_rows = (
        await db.execute(
            select(TdsDeduction, TdsDeductionReversal.id).outerjoin(TdsDeductionReversal, TdsDeductionReversal.tds_deduction_id == TdsDeduction.id).where(TdsDeduction.payment_id == p.id)
        )
    ).all()
    nums = await _numbers(db, {target_of(t)[1] for t, _ in tds_rows})
    return PaymentDetailOut(
        **await _payment_out(db, p),
        allocations=await allocations_for(db, payment_id=p.id),
        tds=[tds_out(t, nums.get(target_of(t)[1]), r is not None) for t, r in tds_rows],
    )


async def list_payments(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    *,
    client_id: uuid.UUID | None = None,
    legal_entity_id: uuid.UUID | None = None,
    status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 200,
) -> list[PaymentOut]:
    stmt = select(Payment)
    if client_id is not None:
        stmt = stmt.where(Payment.client_id == client_id)
    if legal_entity_id is not None:
        stmt = stmt.where(Payment.legal_entity_id == legal_entity_id)
    if status is not None:
        stmt = stmt.where(Payment.allocation_status == status)
    if date_from is not None:
        stmt = stmt.where(Payment.payment_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Payment.payment_date <= date_to)
    rows = (await db.execute(stmt.order_by(Payment.payment_date.desc(), Payment.created_at.desc()).limit(limit))).scalars()
    return [PaymentOut(**await _payment_out(db, p)) for p in rows]


async def open_documents(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, legal_entity_id: uuid.UUID | None = None, currency: str | None = None) -> list[OpenDocumentOut]:
    docs = [d for d in await documents(db, client_id=client_id, legal_entity_id=legal_entity_id, currency=currency) if d.status == "issued"]
    bals = await settlement.balances(db, docs)
    profile = None
    try:
        from app.services import commercials_service

        profile = await commercials_service.get_billing_profile(ctx, db, client_id)
    except Exception:
        profile = None
    rate = Decimal(profile.tds_rate_pct) if profile is not None and profile.tds_rate_pct is not None else None
    out = []
    for b in sorted(bals.values(), key=lambda x: (x.doc.due_date or date.max, str(x.doc.id))):
        if b.open <= 0:
            continue
        out.append(
            OpenDocumentOut(
                document_kind=b.doc.kind, document_id=b.doc.id, number=b.doc.number, doc_date=b.doc.doc_date, due_date=b.doc.due_date,
                currency=b.doc.currency, document_total=b.doc.document_total, open_amount=b.open, disputed_amount=b.disputed,
                payment_status=b.status, taxable_excl_gst=b.doc.taxable_total,
                expected_tds=tds_math.expected_tds(b.doc.taxable_total, rate) if rate is not None else None,
                tds_section_code=profile.tds_section_code if profile is not None else None, tds_rate_pct=rate,
                gst_tds_applicable=bool(profile.gst_tds_applicable) if profile is not None else False,
            )  # fmt: skip
        )
    return out


__all__ = ["allocate", "get_payment", "late_date", "list_payments", "open_documents", "record", "reverse", "reverse_allocation"]
