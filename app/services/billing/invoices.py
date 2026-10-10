# ruff: noqa: E501, ARG001
"""Invoice drafts (FR-147, FR-148): create, price, tax-preview, edit manual lines, recompute, discard; read models.

Drafts are never numbered (AD-18). Every write goes through ``refresh`` which recomputes the whole document from
its stored inputs with ``compute.compute_document`` (the only arithmetic) for the preview date and stores the
preview fingerprint the approver sees. Postings are reserved per source item (AD-23) and released on discard /
line removal.
"""

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, fiscal
from app.core.context import RequestContext
from app.core.money import assert_document_currency, to_decimal
from app.models.billing import CreditNote, DebitNote, Invoice, InvoiceLine, InvoiceTaxLine
from app.schemas.billing import (
    DocDetailOut,
    DocLineOut,
    DocSummaryOut,
    FxOverrideIn,
    LinePatch,
    ManualInvoiceIn,
    ManualLineIn,
    TaxLineOut,
)
from app.services import legal_entities_service, postings
from app.services.billing import compute, context, pricing, rates, words
from app.services.billing.common import (
    MODELS,
    TARGET_KIND,
    assert_editable,
    conflict,
    get_doc,
    invalid,
    lines_of,
    not_found,
    settings,
    tax_lines_of,
    tenant_of,
    user_id,
)

ZERO = Decimal("0.00")


@dataclasses.dataclass
class DraftLine:
    line_kind: str
    description: str
    sac_code: str
    quantity: Decimal
    rate: Decimal
    multiplier: Decimal = Decimal(1)
    discount: Decimal = ZERO
    unit: str = "item"
    pure_agent: bool = False
    placement_id: uuid.UUID | None = None
    po_id: uuid.UUID | None = None
    category: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    is_manual: bool = False
    sources: list[postings.SourceItem] = dataclasses.field(default_factory=list)


UQC = {"hour": "HRS", "day": "DAY", "month": "MON", "item": "OTH"}


async def today_for_entity(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID) -> date:
    entity = await legal_entities_service.get_entity(ctx, db, legal_entity_id)
    return fiscal.today_for(entity.timezone)


def line_input(row: Any) -> compute.LineInput:
    return compute.LineInput(
        line_kind=row.line_kind,
        description=row.description,
        sac_code=row.sac_code,
        quantity=Decimal(row.quantity),
        rate=Decimal(row.rate),
        multiplier=Decimal(row.multiplier),
        discount=Decimal(row.discount_amount),
        unit=row.unit,
        pure_agent=bool(row.outside_taxable_value),
    )


@dataclasses.dataclass
class Recomputed:
    supply: context.Supply
    fx: context.Fx
    calc: compute.DocCalc
    fingerprint: str
    rates: dict[str, Decimal]


async def recompute(
    ctx: RequestContext, db: AsyncSession, doc: Invoice, line_rows: Sequence[Any], on_date: date
) -> Recomputed:
    """Supply, rates, pricing and FX for ``on_date`` from the stored line inputs (no writes)."""
    supply = await context.supply_for(
        ctx,
        db,
        legal_entity_id=doc.legal_entity_id,
        gst_registration_id=doc.supplier_gst_registration_id,
        client_id=doc.client_id,
        contract_id=doc.contract_id,
        doc_date=on_date,
    )
    if doc.fx_source and doc.fx_source.startswith("manual") and doc.fx_rate is not None:
        fx = context.Fx(Decimal(doc.fx_rate), doc.fx_source, doc.fx_rate_date or on_date)
    else:
        fx = await context.fx_for(ctx, db, doc.currency, on_date)
    found = await rates.rates_for(db, {r.sac_code for r in line_rows}, on_date)
    mode = (await settings(db)).round_off_mode
    try:
        calc = compute.compute_document(
            [line_input(r) for r in line_rows],
            found,
            supply.decision,
            doc.currency,
            fx.rate,
            round_off_mode=mode,
        )
    except pricing.PricingError as exc:
        raise invalid(exc.code, str(exc)) from exc
    return Recomputed(supply, fx, calc, compute.fingerprint(supply.decision, calc, fx.rate), found)


async def write_calc(db: AsyncSession, kind: str, doc: Any, line_rows: Sequence[Any], r: Recomputed) -> None:
    """Store a recomputation on the header, lines and tax lines (draft only; the DB guard enforces it)."""
    _m, _l, tax_model, fk = MODELS[kind]
    d = r.supply.decision
    doc.recipient_snapshot = r.supply.recipient
    doc.supplier_snapshot = r.supply.supplier
    doc.billing_address_id = r.supply.billing_address_id
    doc.pos_state_code = d.pos_state_code
    doc.supply_type = d.supply_type
    doc.intra_state = d.intra_state
    doc.reverse_charge = d.reverse_charge
    doc.lut_ref = d.lut_ref
    doc.warnings = list(d.warnings)
    doc.fx_rate, doc.fx_source, doc.fx_rate_date = r.fx.rate, r.fx.source, r.fx.rate_date
    c = r.calc
    doc.taxable_total = c.taxable_total
    doc.outside_taxable_total = c.outside_taxable_total
    doc.charged_tax_total = c.charged_tax_total
    doc.rcm_tax_total = c.rcm_tax_total
    doc.round_off = c.round_off
    doc.document_total = c.document_total
    doc.inr_taxable_total = c.inr_taxable_total
    doc.inr_tax_total = c.inr_tax_total
    doc.inr_document_total = c.inr_document_total
    doc.preview_fingerprint = r.fingerprint
    await db.execute(delete(tax_model).where(getattr(tax_model, fk) == doc.id))
    tid = tenant_of(db)
    for row, lc in zip(line_rows, c.lines, strict=True):
        row.effective_rate = lc.effective_rate
        row.gross_amount = lc.gross
        row.discount_amount = lc.discount
        row.taxable_value = lc.taxable
        row.tax_rate_pct = lc.rate_pct
        row.inr_taxable_value = lc.inr_taxable
        for comp in lc.components:
            db.add(
                tax_model(
                    id=uuid.uuid4(),
                    tenant_id=tid,
                    line_id=row.id,
                    component=comp.component,
                    rate_pct=comp.rate_pct,
                    amount=comp.amount,
                    charged=comp.charged,
                    inr_amount=lc.inr_components.get(comp.component, ZERO),
                    **{fk: doc.id},
                )
            )
    await db.flush()


async def refresh(
    ctx: RequestContext, db: AsyncSession, invoice: Invoice, *, on_date: date | None = None
) -> Recomputed:
    on = on_date or await today_for_entity(ctx, db, invoice.legal_entity_id)
    rows = await lines_of(db, "invoice", invoice.id)
    r = await recompute(ctx, db, invoice, rows, on)
    invoice.preview_date = on
    invoice.stale = False
    if invoice.approved and invoice.approved_fingerprint != r.fingerprint:
        invoice.approved = False
    await write_calc(db, "invoice", invoice, rows, r)
    return r


async def _next_line_no(db: AsyncSession, invoice_id: uuid.UUID) -> int:
    current = (
        await db.execute(select(func.max(InvoiceLine.line_no)).where(InvoiceLine.invoice_id == invoice_id))
    ).scalar()
    return int(current or 0) + 1


async def add_lines(
    ctx: RequestContext, db: AsyncSession, invoice: Invoice, lines: Sequence[DraftLine]
) -> list[InvoiceLine]:
    tid = tenant_of(db)
    out: list[InvoiceLine] = []
    no = await _next_line_no(db, invoice.id)
    for dl in lines:
        if dl.quantity < 0 or dl.rate < 0:
            raise invalid("negative_line", "Invoice lines cannot be negative")
        row = InvoiceLine(
            id=uuid.uuid4(),
            tenant_id=tid,
            invoice_id=invoice.id,
            line_no=no,
            line_kind=dl.line_kind,
            placement_id=dl.placement_id,
            po_id=dl.po_id,
            description=dl.description[:500],
            sac_code=dl.sac_code,
            category=dl.category,
            quantity=dl.quantity,
            unit=dl.unit,
            uqc=UQC.get(dl.unit, "OTH"),
            rate=dl.rate,
            multiplier=dl.multiplier,
            effective_rate=pricing.effective_rate(dl.rate, dl.multiplier),
            gross_amount=ZERO,
            discount_amount=dl.discount,
            taxable_value=ZERO,
            outside_taxable_value=dl.pure_agent,
            is_manual=dl.is_manual,
            period_start=dl.period_start,
            period_end=dl.period_end,
        )
        db.add(row)
        out.append(row)
        no += 1
    await db.flush()
    plines = []
    for dl, row in zip(lines, out, strict=True):
        for item in dl.sources:
            amount = (
                item.amount
                if item.amount is not None
                else pricing.line_amount(abs(item.quantity), dl.rate, dl.multiplier, invoice.currency)
            )
            plines.append(
                postings.PostingLine(
                    item.source_kind,
                    item.source_id,
                    "invoice_line",
                    row.id,
                    invoice.id,
                    Decimal(item.quantity),
                    Decimal(amount),
                    invoice.currency,
                    item.placement_id,
                )
            )
    if plines:
        await postings.reserve(ctx, db, "billing", plines)
    return out


async def create_draft(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    legal_entity_id: uuid.UUID,
    gst_registration_id: uuid.UUID | None,
    client_id: uuid.UUID,
    contract_id: uuid.UUID | None,
    currency: str,
    lines: Sequence[DraftLine],
    run_id: uuid.UUID | None = None,
    notes: str | None = None,
) -> Invoice:
    assert_document_currency(currency)
    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        legal_entity_id=legal_entity_id,
        supplier_gst_registration_id=gst_registration_id,
        client_id=client_id,
        contract_id=contract_id,
        currency=currency.upper(),
        status="draft",
        invoice_run_id=run_id,
        notes=notes,
    )
    db.add(invoice)
    await db.flush()
    await add_lines(ctx, db, invoice, lines)
    await refresh(ctx, db, invoice)
    audit.record(
        "invoice.draft_created", "invoice", invoice.id, session=db, run_id=str(run_id) if run_id else None
    )
    return invoice


# ---------------------------------------------------------------- manual drafts / line edits
async def _manual_line(db: AsyncSession, data: ManualLineIn) -> DraftLine:
    kind = "pure_agent_expense" if data.pure_agent else "manual"
    return DraftLine(
        line_kind=kind,
        description=data.description,
        sac_code=data.sac_code or await rates.default_sac(db, kind),
        quantity=to_decimal(data.quantity),
        rate=to_decimal(data.rate),
        discount=to_decimal(data.discount_amount),
        unit=data.unit or "item",
        pure_agent=data.pure_agent,
        po_id=data.po_id,
        is_manual=True,
    )


async def create_manual(ctx: RequestContext, db: AsyncSession, data: ManualInvoiceIn) -> DocDetailOut:
    await legal_entities_service.assert_accepts_new_documents(ctx, db, data.legal_entity_id)
    reg = data.gst_registration_id
    if reg is None:
        reg = await legal_entities_service.default_registration_for(ctx, db, data.legal_entity_id, None)
    lines = [await _manual_line(db, li) for li in data.lines]
    invoice = await create_draft(
        ctx,
        db,
        legal_entity_id=data.legal_entity_id,
        gst_registration_id=reg,
        client_id=data.client_id,
        contract_id=data.contract_id,
        currency=data.currency,
        lines=lines,
        notes=data.notes,
    )
    return await detail(ctx, db, "invoice", invoice.id)


async def _editable(db: AsyncSession, invoice_id: uuid.UUID) -> Invoice:
    invoice = await get_doc(db, "invoice", invoice_id, lock=True)
    assert_editable(invoice)
    if invoice.status == "pending_approval":  # editing returns the document to draft (approval restarts)
        invoice.status = "draft"
        invoice.approved = False
    return invoice  # type: ignore[no-any-return]


async def add_manual_line(
    ctx: RequestContext, db: AsyncSession, invoice_id: uuid.UUID, data: ManualLineIn
) -> DocDetailOut:
    invoice = await _editable(db, invoice_id)
    await add_lines(ctx, db, invoice, [await _manual_line(db, data)])
    await refresh(ctx, db, invoice)
    return await detail(ctx, db, "invoice", invoice.id)


async def _line(db: AsyncSession, invoice_id: uuid.UUID, line_id: uuid.UUID) -> InvoiceLine:
    row = (
        await db.execute(
            select(InvoiceLine).where(InvoiceLine.id == line_id, InvoiceLine.invoice_id == invoice_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("invoice line")
    return row


async def patch_line(
    ctx: RequestContext, db: AsyncSession, invoice_id: uuid.UUID, line_id: uuid.UUID, data: LinePatch
) -> DocDetailOut:
    invoice = await _editable(db, invoice_id)
    row = await _line(db, invoice_id, line_id)
    fields = data.model_dump(exclude_unset=True)
    if ("quantity" in fields or "rate" in fields or "description" in fields) and not row.is_manual:
        raise invalid("line_not_manual", "Only manual lines can change quantity, rate or description")
    for key, value in fields.items():
        if key == "discount_amount":
            if value is None or Decimal(value) < 0:
                raise invalid("negative_line", "A discount cannot be negative")
            row.discount_amount = Decimal(value)
        elif value is not None or key == "po_id":
            setattr(row, key, value)
    await db.flush()
    await refresh(ctx, db, invoice)
    return await detail(ctx, db, "invoice", invoice.id)


async def remove_line(
    ctx: RequestContext, db: AsyncSession, invoice_id: uuid.UUID, line_id: uuid.UUID
) -> DocDetailOut:
    invoice = await _editable(db, invoice_id)
    row = await _line(db, invoice_id, line_id)
    ids = [
        p.id
        for p in await postings.sources_for(db, "billing", "invoice_line", row.id)
        if p.state != "released"
    ]
    if ids:
        await postings.release(ctx, db, "billing", reason="line_removed", posting_ids=ids)
    await db.execute(delete(InvoiceTaxLine).where(InvoiceTaxLine.line_id == row.id))
    await db.delete(row)
    await db.flush()
    await refresh(ctx, db, invoice)
    return await detail(ctx, db, "invoice", invoice.id)


async def recompute_draft(ctx: RequestContext, db: AsyncSession, invoice_id: uuid.UUID) -> DocDetailOut:
    invoice = await _editable(db, invoice_id)
    await refresh(ctx, db, invoice)
    return await detail(ctx, db, "invoice", invoice.id)


async def set_fx(
    ctx: RequestContext, db: AsyncSession, invoice_id: uuid.UUID, data: FxOverrideIn
) -> DocDetailOut:
    invoice = await _editable(db, invoice_id)
    if invoice.currency == "INR":
        raise invalid("fx_not_applicable", "INR documents have no exchange rate")
    rate = to_decimal(data.fx_rate)
    if rate <= 0:
        raise invalid("fx_rate_invalid", "The rate must be positive")
    invoice.fx_rate = rate
    invoice.fx_source = f"manual:{data.source}"[:60]
    invoice.fx_rate_date = await today_for_entity(ctx, db, invoice.legal_entity_id)
    await refresh(ctx, db, invoice)
    return await detail(ctx, db, "invoice", invoice.id)


async def discard(ctx: RequestContext, db: AsyncSession, invoice_id: uuid.UUID) -> DocDetailOut:
    invoice = await get_doc(db, "invoice", invoice_id, lock=True)
    if invoice.status == "discarded":
        return await detail(ctx, db, "invoice", invoice.id)
    assert_editable(invoice)
    await postings.release(ctx, db, "billing", reason="draft_discarded", document_id=invoice.id)
    if invoice.approval_request_id is not None:
        from app.services.approvals import service as approvals

        await approvals.cancel_open(ctx, db, "invoice", invoice.id)
    invoice.status = "discarded"
    await db.flush()
    audit.record("invoice.discarded", "invoice", invoice.id, session=db, by=str(user_id(ctx)))
    return await detail(ctx, db, "invoice", invoice.id)


# ---------------------------------------------------------------- read models
def _summary_fields(kind: str, doc: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "original_invoice_id": getattr(doc, "original_invoice_id", None),
        "invoice_run_id": getattr(doc, "invoice_run_id", None),
        "client_name": (doc.recipient_snapshot or {}).get("display_name")
        or (doc.recipient_snapshot or {}).get("name"),
    }


def summary(kind: str, doc: Any) -> DocSummaryOut:
    data = DocSummaryOut.model_validate(doc).model_dump()
    data.update(_summary_fields(kind, doc))
    return DocSummaryOut(**data)


async def detail(ctx: RequestContext, db: AsyncSession, kind: str, doc_id: uuid.UUID) -> DocDetailOut:
    doc = await get_doc(db, kind, doc_id)
    lines = await lines_of(db, kind, doc_id)
    taxes = await tax_lines_of(db, kind, doc_id)
    by_line: dict[uuid.UUID, list[TaxLineOut]] = {}
    for t in sorted(taxes, key=lambda x: ("cgst", "sgst", "utgst", "igst").index(x.component)):
        by_line.setdefault(t.line_id, []).append(TaxLineOut.model_validate(t))
    out_lines = []
    for row in lines:
        lo = DocLineOut.model_validate(row)
        lo.taxes = by_line.get(row.id, [])
        out_lines.append(lo)
    data = DocDetailOut.model_validate(doc).model_dump()
    data.update(_summary_fields(kind, doc))
    data["lines"] = out_lines
    data["amount_in_words"] = words.amount_in_words(Decimal(doc.document_total), doc.currency)
    data["reason"] = getattr(doc, "reason", None)
    data["rebill"] = getattr(doc, "rebill", None)
    data["gst_adjusted"] = getattr(doc, "gst_adjusted", None)
    if kind == "invoice" and doc.ack_at is not None and doc.status == "issued":
        data["irn_cancel_deadline"] = doc.ack_at + timedelta(hours=24)
    return DocDetailOut(**data)


async def list_documents(
    ctx: RequestContext,
    db: AsyncSession,
    kind: str = "invoice",
    *,
    status: str | None = None,
    einvoice_status: str | None = None,
    delivery_status: str | None = None,
    client_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    original_invoice_id: uuid.UUID | None = None,
    limit: int = 200,
) -> list[DocSummaryOut]:
    model = {"invoice": Invoice, "credit_note": CreditNote, "debit_note": DebitNote}[kind]
    stmt = select(model)
    if status:
        stmt = stmt.where(model.status.in_(status.split(",")))
    if einvoice_status:
        stmt = stmt.where(model.einvoice_status == einvoice_status)
    if delivery_status:
        stmt = stmt.where(model.delivery_status == delivery_status)
    if client_id:
        stmt = stmt.where(model.client_id == client_id)
    if run_id and kind == "invoice":
        stmt = stmt.where(Invoice.invoice_run_id == run_id)
    if original_invoice_id and kind != "invoice":
        stmt = stmt.where(model.original_invoice_id == original_invoice_id)
    if date_from:
        stmt = stmt.where(func.coalesce(model.doc_date, model.preview_date) >= date_from)
    if date_to:
        stmt = stmt.where(func.coalesce(model.doc_date, model.preview_date) <= date_to)
    rows = await db.execute(stmt.order_by(model.created_at.desc()).limit(min(limit, 1000)))
    return [summary(kind, r) for r in rows.scalars()]


async def sources(
    ctx: RequestContext, db: AsyncSession, kind: str, doc_id: uuid.UUID
) -> list[dict[str, Any]]:
    await get_doc(db, kind, doc_id)
    out: list[dict[str, Any]] = []
    for row in await lines_of(db, kind, doc_id):
        for p in await postings.sources_for(db, "billing", TARGET_KIND[kind], row.id):
            out.append(
                {
                    "line_id": str(row.id),
                    "line_no": row.line_no,
                    "source_kind": p.source_kind,
                    "source_id": str(p.source_id),
                    "quantity": str(p.quantity),
                    "amount": str(p.amount),
                    "state": p.state,
                }
            )
    return out


__all__ = ["conflict"]
