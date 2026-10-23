# ruff: file-ignore[line-too-long]
"""Credit Notes and Debit Notes (FR-150). Note lines copy the original line's SAC, tax rates, supply decision and the
Invoice ``fx_rate`` verbatim: tax is never re-determined.

Caps (per line and per tax component, checked again at issue under the original Invoice's row lock): credited
<= original + debited. The note that exhausts a line takes the **remainder** of the taxable value and of each
component (and of the INR amounts). A full Credit Note cancels the Invoice (``full_credit_note``) in the same
transaction and releases its postings unless ``rebill=false``; a partial note never releases (AD-23).
"""

import dataclasses
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.events import emit
from app.core.money import MONEY_CONTEXT, quantize_amount, to_decimal
from app.events.billing import CreditNoteIssued, DebitNoteIssued, InvoiceCancelled
from app.models.billing import (
    CreditNote,
    CreditNoteLine,
    CreditNoteTaxLine,
    DebitNote,
    DebitNoteLine,
    DebitNoteTaxLine,
    Invoice,
    InvoiceLine,
    InvoiceTaxLine,
)
from app.schemas.billing import CapComponent, CreditNoteIn, DebitNoteIn, DocDetailOut, LineCap, NoteCapsOut
from app.services import legal_entities_service, postings
from app.services.billing import compute, dsc, invoices, issue, pricing
from app.services.billing.common import (
    conflict,
    fy_end,
    get_doc,
    invalid,
    lines_of,
    now,
    settings,
    tenant_of,
    user_id,
)

ZERO = Decimal("0.00")
MANUAL_PARTIAL_REASONS = ("discount", "commercial", "other")
NO_REBILL_REASONS = ("discount", "commercial")
CORRECTION_REASONS = ("hours_correction", "rate_correction")


@dataclasses.dataclass
class Remaining:
    line: InvoiceLine
    taxable: Decimal
    quantity: Decimal
    components: dict[str, Decimal]  # remaining amount per component
    inr_taxable: Decimal
    inr_components: dict[str, Decimal]
    rates: dict[str, Decimal]
    charged: dict[str, bool]
    original_taxable: Decimal
    original_components: dict[str, Decimal]


@dataclasses.dataclass
class NoteLineSpec:
    original: InvoiceLine
    taxable: Decimal
    quantity: Decimal
    rate: Decimal
    multiplier: Decimal = Decimal(1)
    description: str | None = None
    sources: list[postings.SourceItem] = dataclasses.field(default_factory=list)
    no_tax: bool = False


async def remaining(
    db: AsyncSession, invoice: Invoice, *, exclude_note_id: uuid.UUID | None = None
) -> dict[uuid.UUID, Remaining]:
    lines = list(
        (
            await db.execute(
                select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id).order_by(InvoiceLine.line_no)
            )
        ).scalars()
    )
    taxes = list(
        (await db.execute(select(InvoiceTaxLine).where(InvoiceTaxLine.invoice_id == invoice.id))).scalars()
    )
    out: dict[uuid.UUID, Remaining] = {}
    for ln in lines:
        own = [t for t in taxes if t.line_id == ln.id]
        out[ln.id] = Remaining(
            line=ln,
            taxable=Decimal(ln.taxable_value),
            quantity=Decimal(ln.quantity),
            components={t.component: Decimal(t.amount) for t in own},
            inr_taxable=Decimal(ln.inr_taxable_value),
            inr_components={t.component: Decimal(t.inr_amount) for t in own},
            rates={t.component: Decimal(t.rate_pct) for t in own},
            charged={t.component: bool(t.charged) for t in own},
            original_taxable=Decimal(ln.taxable_value),
            original_components={t.component: Decimal(t.amount) for t in own},
        )
    for sign, head, line_model, tax_model, fk in (
        (-1, CreditNote, CreditNoteLine, CreditNoteTaxLine, "credit_note_id"),
        (1, DebitNote, DebitNoteLine, DebitNoteTaxLine, "debit_note_id"),
    ):
        stmt = (
            select(line_model)
            .join(head, head.id == getattr(line_model, fk))
            .where(head.original_invoice_id == invoice.id, head.status.in_(("issued", "cancelled")))
        )
        if exclude_note_id is not None:
            stmt = stmt.where(head.id != exclude_note_id)
        note_lines = list((await db.execute(stmt)).scalars())
        if not note_lines:
            continue
        tax_rows = list(
            (
                await db.execute(select(tax_model).where(tax_model.line_id.in_([n.id for n in note_lines])))
            ).scalars()
        )
        tax_by_line: dict[uuid.UUID, list[Any]] = defaultdict(list)
        for t in tax_rows:
            tax_by_line[t.line_id].append(t)
        for n in note_lines:
            rem = out.get(n.original_invoice_line_id)
            if rem is None:
                continue
            rem.taxable += sign * Decimal(n.taxable_value)
            rem.quantity += sign * Decimal(n.quantity)
            rem.inr_taxable += sign * Decimal(n.inr_taxable_value)
            for t in tax_by_line[n.id]:
                rem.components[t.component] = rem.components.get(t.component, ZERO) + sign * Decimal(t.amount)
                rem.inr_components[t.component] = rem.inr_components.get(t.component, ZERO) + sign * Decimal(
                    t.inr_amount
                )
    return out


def _pct(amount: Decimal, rate: Decimal, currency: str) -> Decimal:
    return quantize_amount(MONEY_CONTEXT.divide(MONEY_CONTEXT.multiply(amount, rate), Decimal(100)), currency)


def credit_components(
    rem: Remaining, taxable: Decimal, currency: str, fx: Decimal
) -> tuple[list[tuple[str, Decimal, Decimal, bool, Decimal]], Decimal]:
    """Components ``(component, rate, amount, charged, inr_amount)`` and INR taxable of a credit of ``taxable``
    against ``rem``; the credit that exhausts the line takes the remainders. 422 when a cap is exceeded."""
    if taxable > rem.taxable or taxable <= 0:
        raise invalid("credit_exceeds_original", "The credit exceeds what remains on the original line",
                      line_id=str(rem.line.id), remaining=str(rem.taxable), requested=str(taxable))  # fmt: skip
    exhausts = taxable == rem.taxable
    inr_taxable = rem.inr_taxable if exhausts else compute.inr(taxable, fx)
    out = []
    for comp, rate in rem.rates.items():
        amount = rem.components.get(comp, ZERO) if exhausts else _pct(taxable, rate, currency)
        inr_amount = rem.inr_components.get(comp, ZERO) if exhausts else _pct(inr_taxable, rate, "INR")
        if amount > rem.components.get(comp, ZERO) or amount < 0:
            raise invalid("credit_exceeds_original", "The credit exceeds the remaining tax of the original line",
                          line_id=str(rem.line.id), component=comp)  # fmt: skip
        out.append((comp, rate, amount, rem.charged.get(comp, True), inr_amount))
    return out, inr_taxable


def debit_components(
    rem: Remaining, taxable: Decimal, currency: str, fx: Decimal
) -> tuple[list[tuple[str, Decimal, Decimal, bool, Decimal]], Decimal]:
    if taxable <= 0:
        raise invalid("negative_line", "A debit must be positive")
    inr_taxable = compute.inr(taxable, fx)
    return [
        (c, r, _pct(taxable, r, currency), rem.charged.get(c, True), _pct(inr_taxable, r, "INR"))
        for c, r in rem.rates.items()
    ], inr_taxable


def deadline_for(invoice: Invoice, cn_gst_deadline: str) -> date | None:
    if not invoice.fiscal_year:
        return None
    end = fy_end(invoice.fiscal_year)
    month, day = (int(x) for x in cn_gst_deadline.split("-"))
    return date(end.year, month, day)


# ---------------------------------------------------------------- create drafts
async def _issued_invoice(db: AsyncSession, invoice_id: uuid.UUID, *, lock: bool = False) -> Invoice:
    invoice: Invoice = await get_doc(db, "invoice", invoice_id, lock=lock)
    if invoice.status != "issued":
        raise conflict(
            "invalid_state", "Notes can only be raised on an issued invoice", status=invoice.status
        )
    return invoice


async def _write_note(
    ctx: RequestContext,
    db: AsyncSession,
    kind: str,
    invoice: Invoice,
    specs: list[NoteLineSpec],
    *,
    reason: str,
    rebill: bool = False,
    gst_adjusted: bool = True,
    notes: str | None = None,
) -> Any:
    head_model, line_model = (
        (CreditNote, CreditNoteLine) if kind == "credit_note" else (DebitNote, DebitNoteLine)
    )
    tid = tenant_of(db)
    copy = {
        k: getattr(invoice, k)
        for k in (
            "legal_entity_id", "supplier_gst_registration_id", "client_id", "currency", "billing_profile_id",
            "billing_address_id", "contract_id", "recipient_snapshot", "supplier_snapshot", "pos_state_code",
            "supply_type", "intra_state", "reverse_charge", "lut_ref", "fx_rate", "fx_source", "fx_rate_date",
        )
    }  # fmt: skip
    extra: dict[str, Any] = {"reason": reason}
    if kind == "credit_note":
        extra.update(rebill=rebill and reason not in NO_REBILL_REASONS, gst_adjusted=gst_adjusted)
    note = head_model(
        id=uuid.uuid4(),
        tenant_id=tid,
        original_invoice_id=invoice.id,
        status="draft",
        notes=notes,
        **copy,
        **extra,
    )
    note.preview_date = await invoices.today_for_entity(ctx, db, invoice.legal_entity_id)
    db.add(note)
    await db.flush()
    fk = "credit_note_id" if kind == "credit_note" else "debit_note_id"
    plines = []
    for no, spec in enumerate(specs, start=1):
        o = spec.original
        row = line_model(
            id=uuid.uuid4(),
            tenant_id=tid,
            line_no=no,
            line_kind=o.line_kind if not spec.no_tax else "manual",
            placement_id=o.placement_id,
            po_id=o.po_id,
            description=(spec.description or o.description)[:500],
            sac_code=o.sac_code,
            category=o.category,
            quantity=spec.quantity,
            unit=o.unit,
            uqc=o.uqc,
            rate=spec.rate,
            multiplier=spec.multiplier,
            effective_rate=pricing.effective_rate(spec.rate, spec.multiplier),
            gross_amount=spec.taxable,
            taxable_value=spec.taxable,
            outside_taxable_value=o.outside_taxable_value,
            tax_rate_pct=ZERO if spec.no_tax else o.tax_rate_pct,
            period_start=o.period_start,
            period_end=o.period_end,
            original_invoice_line_id=o.id,
            is_manual=spec.no_tax,
            **{fk: note.id},
        )
        db.add(row)
        for item in spec.sources:
            plines.append(
                postings.PostingLine(
                    item.source_kind,
                    item.source_id,
                    f"{kind}_line",
                    row.id,
                    note.id,
                    Decimal(item.quantity),
                    abs(spec.taxable),
                    invoice.currency,
                    item.placement_id,
                )
            )
    await db.flush()
    if plines:
        await postings.reserve(ctx, db, "billing", plines)
    await _finalize(db, kind, note, invoice)
    return note


async def _finalize(db: AsyncSession, kind: str, note: Any, invoice: Invoice) -> bool:
    """(Re)compute the note's tax lines and totals from its line taxable values; returns True when the
    credit note exhausts every line of the invoice (full)."""
    _head, line_model, tax_model, fk = (
        (CreditNote, CreditNoteLine, CreditNoteTaxLine, "credit_note_id")
        if kind == "credit_note"
        else (DebitNote, DebitNoteLine, DebitNoteTaxLine, "debit_note_id")
    )
    from sqlalchemy import delete

    rows = list(
        (
            await db.execute(
                select(line_model).where(getattr(line_model, fk) == note.id).order_by(line_model.line_no)
            )
        ).scalars()
    )
    await db.execute(delete(tax_model).where(getattr(tax_model, fk) == note.id))
    rem = await remaining(db, invoice, exclude_note_id=note.id)
    fx = Decimal(invoice.fx_rate or 1)
    used: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    taxable_total = outside = charged = rcm = inr_taxable_total = inr_tax = ZERO
    for row in rows:
        r = rem[row.original_invoice_line_id]
        # several note lines on one original line: cap against what earlier lines of this note left
        r_eff = dataclasses.replace(r, taxable=r.taxable - used[r.line.id])
        taxable = Decimal(row.taxable_value)
        if row.is_manual and kind == "credit_note" and not note.gst_adjusted:
            comps, inr_taxable = [], compute.inr(taxable, fx)
        elif kind == "credit_note":
            comps, inr_taxable = credit_components(r_eff, taxable, note.currency, fx)
        else:
            comps, inr_taxable = debit_components(r, taxable, note.currency, fx)
        used[r.line.id] += taxable
        row.inr_taxable_value = inr_taxable
        if row.outside_taxable_value:
            outside += taxable
        else:
            taxable_total += taxable
            inr_taxable_total += inr_taxable
        for comp, rate, amount, is_charged, inr_amount in comps:
            db.add(tax_model(id=uuid.uuid4(), tenant_id=note.tenant_id, line_id=row.id, component=comp, rate_pct=rate,
                             amount=amount, charged=is_charged, inr_amount=inr_amount, **{fk: note.id}))  # fmt: skip
            if is_charged:
                charged += amount
                inr_tax += inr_amount
            else:
                rcm += amount
    full = kind == "credit_note" and all(r.taxable - used[lid] == 0 for lid, r in rem.items())
    note.taxable_total = quantize_amount(taxable_total, note.currency)
    note.outside_taxable_total = quantize_amount(outside, note.currency)
    note.charged_tax_total = quantize_amount(charged, note.currency)
    note.rcm_tax_total = quantize_amount(rcm, note.currency)
    round_off = ZERO
    if full:
        prior = (
            await db.execute(
                select(func.coalesce(func.sum(CreditNote.round_off), 0)).where(
                    CreditNote.original_invoice_id == invoice.id,
                    CreditNote.status == "issued",
                    CreditNote.id != note.id,
                )
            )
        ).scalar()
        round_off = Decimal(invoice.round_off) - Decimal(prior or 0)
    note.round_off = round_off
    note.document_total = quantize_amount(taxable_total + outside + charged + round_off, note.currency)
    note.inr_taxable_total = inr_taxable_total
    note.inr_tax_total = inr_tax
    inr_round = round_off if note.currency == "INR" else ZERO
    note.inr_document_total = quantize_amount(
        inr_taxable_total + compute.inr(outside, fx) + inr_tax + inr_round, "INR"
    )
    if kind == "credit_note":
        note.is_full = full
    await db.flush()
    return full


async def create_credit_note(
    ctx: RequestContext, db: AsyncSession, invoice_id: uuid.UUID, data: CreditNoteIn, *, system: bool = False
) -> DocDetailOut:
    invoice = await _issued_invoice(db, invoice_id)
    if invoice.is_imported:
        raise conflict(
            "imported_invoice_no_credit_note", "Credit notes cannot be raised on imported opening invoices"
        )
    rem = await remaining(db, invoice)
    specs: list[NoteLineSpec] = []
    if not data.gst_adjusted:
        if data.amount is None or to_decimal(data.amount) <= 0:
            raise invalid("amount_required", "A commercial credit needs a positive amount")
        first = next(iter(rem.values())).line
        amount = quantize_amount(to_decimal(data.amount), invoice.currency)
        specs.append(
            NoteLineSpec(
                first,
                amount,
                Decimal(1),
                amount,
                description="Commercial credit (not a GST adjustment)",
                no_tax=True,
            )
        )
    elif data.full:
        specs = [
            NoteLineSpec(r.line, r.taxable, r.quantity, Decimal(r.line.rate), Decimal(r.line.multiplier))
            for r in rem.values()
            if r.taxable > 0
        ]
        if not specs:
            raise conflict("nothing_to_credit", "Every line of this invoice is already credited")
    else:
        if data.reason not in MANUAL_PARTIAL_REASONS and not system:
            raise invalid("reason_not_allowed", "Hours and rate corrections come from approved adjustments")
        if not data.lines:
            raise invalid("lines_required", "Pick at least one line to credit")
        for nl in data.lines:
            r = rem.get(nl.original_invoice_line_id)
            if r is None:
                raise invalid("line_not_on_invoice", "The line is not on this invoice")
            if nl.amount is not None:
                taxable = quantize_amount(to_decimal(nl.amount), invoice.currency)
                qty = Decimal(0)
            elif nl.quantity is not None:
                qty = to_decimal(nl.quantity)
                taxable = quantize_amount(
                    MONEY_CONTEXT.multiply(qty, Decimal(r.line.effective_rate)), invoice.currency
                )
            else:
                raise invalid("quantity_or_amount", "Give a quantity or an amount")
            specs.append(
                NoteLineSpec(
                    r.line,
                    taxable,
                    qty,
                    Decimal(r.line.rate) if qty else taxable,
                    Decimal(r.line.multiplier) if qty else Decimal(1),
                )
            )
    note = await _write_note(
        ctx,
        db,
        "credit_note",
        invoice,
        specs,
        reason=data.reason,
        rebill=data.rebill,
        gst_adjusted=data.gst_adjusted,
        notes=data.notes,
    )
    audit.record("credit_note.draft_created", "credit_note", note.id, session=db, invoice_id=str(invoice.id))
    return await invoices.detail(ctx, db, "credit_note", note.id)


async def create_debit_note(
    ctx: RequestContext, db: AsyncSession, invoice_id: uuid.UUID, data: DebitNoteIn
) -> DocDetailOut:
    invoice = await _issued_invoice(db, invoice_id)
    rem = await remaining(db, invoice)
    specs = []
    for nl in data.lines:
        r = rem.get(nl.original_invoice_line_id)
        if r is None:
            raise invalid("line_not_on_invoice", "The line is not on this invoice")
        if nl.amount is not None:
            taxable, qty, rate = (
                quantize_amount(to_decimal(nl.amount), invoice.currency),
                Decimal(0),
                quantize_amount(to_decimal(nl.amount), invoice.currency),
            )
        elif nl.quantity is not None:
            qty = to_decimal(nl.quantity)
            taxable, rate = (
                quantize_amount(
                    MONEY_CONTEXT.multiply(qty, Decimal(r.line.effective_rate)), invoice.currency
                ),
                Decimal(r.line.rate),
            )
        else:
            raise invalid("quantity_or_amount", "Give a quantity or an amount")
        specs.append(
            NoteLineSpec(r.line, taxable, qty, rate, Decimal(r.line.multiplier) if qty else Decimal(1))
        )
    note = await _write_note(ctx, db, "debit_note", invoice, specs, reason=data.reason, notes=data.notes)
    return await invoices.detail(ctx, db, "debit_note", note.id)


async def note_caps(ctx: RequestContext, db: AsyncSession, invoice_id: uuid.UUID) -> NoteCapsOut:
    invoice = await get_doc(db, "invoice", invoice_id)
    rem = await remaining(db, invoice)
    cfg = await settings(db)
    deadline = deadline_for(invoice, cfg.cn_gst_deadline)
    today = await invoices.today_for_entity(ctx, db, invoice.legal_entity_id)
    return NoteCapsOut(
        invoice_id=invoice.id,
        currency=invoice.currency,
        gst_deadline=deadline,
        deadline_passed=bool(deadline and today > deadline),
        imported=invoice.is_imported,
        lines=[
            LineCap(
                line_id=r.line.id,
                line_no=r.line.line_no,
                description=r.line.description,
                effective_rate=r.line.effective_rate,
                quantity=r.line.quantity,
                original_taxable=r.original_taxable,
                remaining_taxable=r.taxable,
                remaining_quantity=max(r.quantity, ZERO),
                components=[
                    CapComponent(
                        component=c,
                        original=r.original_components.get(c, ZERO),
                        remaining=r.components.get(c, ZERO),
                    )
                    for c in r.rates
                ],
            )
            for r in rem.values()
        ],
    )


# ---------------------------------------------------------------- issue / discard
async def issue_note(
    ctx: RequestContext, db: AsyncSession, kind: str, note_id: uuid.UUID, doc_date: date | None = None
) -> DocDetailOut:
    require_human(actor_from_ctx(ctx))
    note = await get_doc(db, kind, note_id, lock=True)
    if note.status == "issued":
        return await invoices.detail(ctx, db, kind, note.id)
    if note.status not in ("draft", "pending_approval"):
        raise conflict("invalid_state", "This note cannot be issued", status=note.status)
    invoice: Invoice = await get_doc(
        db, "invoice", note.original_invoice_id, lock=True
    )  # lock order: note -> invoice
    if invoice.status != "issued":
        raise conflict("invalid_state", "The original invoice is not issued", status=invoice.status)
    when = await issue.resolve_doc_date(ctx, db, note, doc_date)
    full = await _finalize(db, kind, note, invoice)
    rows = await lines_of(db, kind, note.id)
    if kind == "credit_note" and note.gst_adjusted and note.charged_tax_total + note.rcm_tax_total > 0:
        deadline = deadline_for(invoice, (await settings(db)).cn_gst_deadline)
        if deadline is not None and when > deadline:
            raise conflict("cn_gst_deadline_passed", "The GST credit-note deadline for the invoice's financial year has passed; issue a commercial credit (gst_adjusted=false) instead",
                           deadline=deadline.isoformat())  # fmt: skip
    if kind == "debit_note":
        await issue._po_consumption(ctx, db, "debit_note", note, rows, 1)
    elif note.reason in CORRECTION_REASONS or (full and note.rebill):
        await issue._po_consumption(ctx, db, "credit_note", note, rows, -1)
    await postings.post(ctx, db, "billing", document_id=note.id)
    reg = None
    if note.supplier_gst_registration_id is not None:
        reg = await legal_entities_service.get_gst_registration(ctx, db, note.supplier_gst_registration_id)
    issue.einvoice_fields(
        note,
        note.supply_type or "",
        bool(reg and reg.einvoice_applicable),
        bool(reg and reg.irp_reporting_limit),
        when,
    )
    if kind == "credit_note" and not note.gst_adjusted:
        note.einvoice_status = "not_applicable"
    if kind == "debit_note":
        from app.services import commercials_service

        note.due_date = await commercials_service.due_date(ctx, db, note.client_id, note.contract_id, when)
    note.prior_period = bool(invoice.fiscal_year and invoice.fiscal_year != _fy(when))
    await issue.assign_number(ctx, db, kind, note, when)
    note.doc_date = when
    note.issued_at = now()
    note.issued_by = user_id(ctx)
    await db.flush()
    digest = await issue.content_hash(db, kind, note)
    note.status = "issued"
    note.content_hash = digest
    dsc.note_marked(kind, note)  # Epic 51: trigger tos_dsc_mark set dsc_status (refreshed by content_hash)
    await db.flush()
    if kind == "credit_note":
        if full:
            invoice.status = "cancelled"
            invoice.cancel_basis = "full_credit_note"
            invoice.cancelled_at = now()
            invoice.cancelling_credit_note_id = note.id
            await db.flush()
            if note.rebill:
                await postings.release(ctx, db, "billing", reason="full_credit_note", document_id=invoice.id)
            emit(
                db,
                InvoiceCancelled(
                    tenant_id=invoice.tenant_id, invoice_id=invoice.id, cancel_basis="full_credit_note"
                ),
            )
        emit(
            db,
            CreditNoteIssued(
                tenant_id=note.tenant_id,
                credit_note_id=note.id,
                invoice_id=invoice.id,
                client_id=note.client_id,
                rebill=bool(note.rebill),
            ),
        )
    else:
        emit(
            db,
            DebitNoteIssued(
                tenant_id=note.tenant_id,
                debit_note_id=note.id,
                invoice_id=invoice.id,
                client_id=note.client_id,
            ),
        )
    audit.record(f"{kind}.issued", kind, note.id, session=db, number=note.number, full=full)
    return await invoices.detail(ctx, db, kind, note.id)


def _fy(d: date) -> str:
    from app.core import fiscal

    return fiscal.fiscal_year(d)


async def discard_note(ctx: RequestContext, db: AsyncSession, kind: str, note_id: uuid.UUID) -> DocDetailOut:
    note = await get_doc(db, kind, note_id, lock=True)
    if note.status not in ("draft", "pending_approval"):
        raise conflict("invalid_state", "Only a draft note can be discarded", status=note.status)
    await postings.release(ctx, db, "billing", reason="draft_discarded", document_id=note.id)
    note.status = "discarded"
    await db.flush()
    return await invoices.detail(ctx, db, kind, note.id)
