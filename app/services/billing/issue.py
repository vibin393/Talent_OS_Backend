# ruff: noqa: E501, ARG001
"""Approval and the issue transaction (FR-148, FR-144; AD-18 / AD-19 / AD-21 / AD-23).

``issue_invoice`` runs in ONE transaction, in this order: lock the invoice -> resolve ``doc_date`` + period check
+ due date -> recompute supply / rates / pricing / FX for ``doc_date`` and compare with what was approved
(409 ``draft_stale``) -> recipient validation -> PO consumption (uuid order) -> ``postings.post`` -> e-invoice
status -> ``numbering.assign`` LAST -> content hash, audit, ``InvoiceIssued``. No external I/O happens inside;
PDF / email / connector work runs after commit.
"""

import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import DomainError, ValidationFailed
from app.core.events import emit
from app.core.money import MONEY_CONTEXT, quantize_amount
from app.events.billing import InvoiceIssued
from app.models.billing import Invoice
from app.schemas.billing import DocDetailOut, IssueBatchOut, IssueResult
from app.services import commercials_service, legal_entities_service, postings
from app.services.billing import compute, dsc, invoices, tax
from app.services.billing.common import (
    MODELS,
    OPERATIONAL,
    conflict,
    get_doc,
    invalid,
    lines_of,
    now,
    user_id,
)

SUBJECT = "invoice"


# ---------------------------------------------------------------- content hash (AD-21)
def _row_dict(row: Any, exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    return {c.name: getattr(row, c.key) for c in row.__table__.columns if c.name not in exclude}


async def content_hash(db: AsyncSession, kind: str, doc: Any) -> str:
    """SHA-256 over canonical JSON of header (minus operational columns), lines and tax lines."""
    await db.flush()
    await db.refresh(doc)  # hash the stored values (DB scale / types), not in-memory ones
    _m, line_model, tax_model, fk = MODELS[kind]
    lines = list(
        (
            await db.execute(
                select(line_model)
                .where(getattr(line_model, fk) == doc.id)
                .order_by(line_model.line_no)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    taxes = list(
        (
            await db.execute(
                select(tax_model)
                .where(getattr(tax_model, fk) == doc.id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    skip = frozenset({"created_at", "updated_at"})
    return compute.sha256(
        {
            "header": _row_dict(doc, OPERATIONAL | skip),
            "lines": [_row_dict(r, skip) for r in lines],
            "taxes": sorted(
                (_row_dict(t, skip) for t in taxes), key=lambda t: (str(t["line_id"]), t["component"])
            ),
        }
    )


# ---------------------------------------------------------------- approval (AD-24)
async def submit(ctx: RequestContext, db: AsyncSession, invoice_id: uuid.UUID) -> DocDetailOut:
    from app.services.approvals import service as approvals

    invoice = await get_doc(db, "invoice", invoice_id, lock=True)
    if invoice.status != "draft":
        raise conflict("invalid_state", "Only a draft can be submitted", status=invoice.status)
    if not await lines_of(db, "invoice", invoice.id):
        raise invalid("invoice_empty", "The invoice has no lines")
    await invoices.refresh(ctx, db, invoice)
    invoice.status = "pending_approval"
    invoice.approved = False
    await db.flush()
    has_manual = any(r.is_manual for r in await lines_of(db, "invoice", invoice.id))
    result = await approvals.start(
        ctx,
        db,
        SUBJECT,
        invoice.id,
        {
            "amount": str(invoice.document_total),
            "inr_amount": str(invoice.inr_document_total),
            "currency": invoice.currency,
            "client_id": str(invoice.client_id),
            "has_manual_lines": has_manual,
        },
    )
    invoice = await get_doc(db, "invoice", invoice_id)
    invoice.approval_request_id = result.request_id
    await db.flush()
    return await invoices.detail(ctx, db, "invoice", invoice.id)


async def on_final_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:
    kind = decision.subject_type
    doc = await get_doc(db, kind, decision.subject_id, lock=True)
    if doc.status != "pending_approval":
        return
    if decision.outcome in ("approved", "auto_approved"):
        doc.approved = True
        doc.approved_fingerprint = doc.preview_fingerprint
    else:
        doc.status = "draft"
        doc.approved = False
    await db.flush()


# ---------------------------------------------------------------- shared issue steps
async def resolve_doc_date(ctx: RequestContext, db: AsyncSession, doc: Any, doc_date: date | None) -> date:
    when = doc_date or await invoices.today_for_entity(ctx, db, doc.legal_entity_id)
    await legal_entities_service.assert_open(db, doc.legal_entity_id, when)
    await legal_entities_service.assert_accepts_new_documents(
        ctx, db, doc.legal_entity_id, require_complete=doc.supplier_gst_registration_id is not None
    )
    return when


async def assign_number(ctx: RequestContext, db: AsyncSession, kind: str, doc: Any, doc_date: date) -> None:
    """Numbering LAST (AD-18): series per registration and document type; back-dating refused."""
    series_id = await legal_entities_service.series_for(
        ctx,
        db,
        legal_entity_id=doc.legal_entity_id,
        gst_registration_id=doc.supplier_gst_registration_id,
        doc_type=kind,
    )
    try:
        number = await legal_entities_service.assign(db, series_id, doc_date)
    except ValidationFailed as exc:
        if exc.code == "doc_date_before_last_issued":
            raise conflict(
                "doc_date_out_of_order",
                "The document date is before the last issued document of this series",
                **(exc.details or {}),
            ) from exc
        raise
    from app.core import fiscal

    doc.series_id = series_id
    doc.fiscal_year = fiscal.fiscal_year(doc_date)
    doc.number = number


def einvoice_fields(doc: Any, supply_type: str, applicable: bool, irp_limit: bool, doc_date: date) -> None:
    if tax.einvoice_applicable(supply_type, applicable):
        doc.einvoice_status = "pending_irn"
        doc.irn_report_by = doc_date + timedelta(days=30) if irp_limit else None
    else:
        doc.einvoice_status = "not_applicable"


async def _po_consumption(
    ctx: RequestContext, db: AsyncSession, kind: str, doc: Any, line_rows: list[Any], sign: int = 1
) -> None:
    per_po: dict[uuid.UUID, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for r in line_rows:
        if r.po_id is None:
            continue
        per_po[r.po_id][0] += Decimal(r.taxable_value)
        if r.unit == "hour":
            per_po[r.po_id][1] += Decimal(r.quantity)
    for po_id in sorted(per_po, key=str):
        amount, hours = per_po[po_id]
        await commercials_service.record_po_consumption(
            ctx, db, po_id, kind, doc.id, amount * sign, (hours * sign) if hours else None, doc.currency
        )


async def _cost_inr(
    ctx: RequestContext, db: AsyncSession, invoice: Invoice, line_rows: list[Any], doc_date: date
) -> None:
    """Note 3: cost frozen at issue = quantity x cost rate x pay multiplier x fx (INR); expenses = amount."""
    from app.services import placements_service, time_service

    fx = Decimal(invoice.fx_rate or 1)
    for r in line_rows:
        cost = Decimal("0.00")
        if r.line_kind in ("expense", "pure_agent_expense"):
            cost = compute.inr(Decimal(r.taxable_value), fx)
        elif r.line_kind in ("time", "adjustment") and r.placement_id is not None:
            try:
                on = r.period_end or doc_date
                pc = await placements_service.pay_context(ctx, db, r.placement_id, on)
                if pc.payee_kind == "vendor":
                    base, burden = pc.vendor_rate, pc.vendor_burden_pct
                else:
                    base, burden = pc.pay_rate, pc.burden_pct
                if base is not None:
                    rate = MONEY_CONTEXT.multiply(
                        Decimal(base), Decimal(1) + Decimal(burden or 0) / Decimal(100)
                    )
                    mult = Decimal(1)
                    try:
                        pi = await time_service.pay_inputs(ctx, db, r.placement_id, r.period_start or on, on)
                        mult = Decimal(pi.pay_multipliers.get(r.category or "regular", 1))
                    except Exception:
                        mult = Decimal(1)
                    hours = Decimal(r.quantity)
                    cost = quantize_amount(
                        MONEY_CONTEXT.multiply(
                            MONEY_CONTEXT.multiply(MONEY_CONTEXT.multiply(hours, rate), mult), fx
                        ),
                        "INR",
                    )
            except DomainError:
                cost = Decimal("0.00")
        r.cost_amount_inr = cost


# ---------------------------------------------------------------- issue
async def issue_invoice(
    ctx: RequestContext, db: AsyncSession, invoice_id: uuid.UUID, doc_date: date | None = None
) -> DocDetailOut:
    require_human(actor_from_ctx(ctx))
    invoice: Invoice = await get_doc(db, "invoice", invoice_id, lock=True)  # 1
    if invoice.status == "issued":
        return await invoices.detail(ctx, db, "invoice", invoice.id)
    if invoice.status not in ("draft", "pending_approval"):
        raise conflict("invalid_state", "This invoice cannot be issued", status=invoice.status)
    if invoice.status == "pending_approval" and not invoice.approved:
        raise conflict("approval_pending", "The invoice is waiting for approval")
    rows = await lines_of(db, "invoice", invoice.id)
    if not rows:
        raise invalid("invoice_empty", "The invoice has no lines")
    when = await resolve_doc_date(ctx, db, invoice, doc_date)  # 2
    expected = invoice.approved_fingerprint if invoice.approved else invoice.preview_fingerprint
    old = {
        "document_total": invoice.document_total,
        "tax": invoice.charged_tax_total,
        "fx_rate": invoice.fx_rate,
        "supply_type": invoice.supply_type,
    }
    r = await invoices.recompute(ctx, db, invoice, rows, when)  # 3
    if invoice.stale or (expected is not None and r.fingerprint != expected):
        new = {
            "document_total": r.calc.document_total,
            "tax": r.calc.charged_tax_total,
            "fx_rate": r.fx.rate,
            "supply_type": r.supply.decision.supply_type,
        }
        raise conflict(
            "draft_stale",
            "Rates, FX or tax changed since the draft was approved; recompute it",
            diff=compute.diff(old, new),
        )
    recipient = r.supply.recipient  # 4
    if r.supply.decision.supply_type in ("b2b", "sezwp", "sezwop"):
        errors = legal_entities_service.validate_gstin(recipient.get("gstin") or "")
        if errors:
            raise invalid(
                "recipient_gstin_invalid", "The recipient GSTIN is not valid", errors=[e.code for e in errors]
            )
    if not recipient.get("name"):
        raise invalid("billing_profile_incomplete", "The billing profile has no legal name")
    await invoices.write_calc(db, "invoice", invoice, rows, r)
    await _cost_inr(ctx, db, invoice, rows, when)
    invoice.due_date = await commercials_service.due_date(
        ctx, db, invoice.client_id, invoice.contract_id, when
    )
    await _po_consumption(ctx, db, "invoice", invoice, rows)  # 5
    await postings.post(ctx, db, "billing", document_id=invoice.id)  # 6
    einvoice_fields(
        invoice, r.supply.decision.supply_type, r.supply.einvoice_registration, r.supply.irp_limit, when
    )  # 7
    await assign_number(ctx, db, "invoice", invoice, when)  # 8 (last)
    invoice.doc_date = when
    invoice.issued_at = now()
    invoice.issued_by = user_id(ctx)
    await db.flush()
    digest = await content_hash(db, "invoice", invoice)  # 9
    invoice.status = "issued"
    invoice.content_hash = digest
    dsc.note_marked("invoice", invoice)  # Epic 51: trigger tos_dsc_mark set dsc_status (refreshed by content_hash)
    await db.flush()
    audit.record(
        "invoice.issued",
        "invoice",
        invoice.id,
        session=db,
        number=invoice.number,
        total=str(invoice.document_total),
    )
    emit(
        db,
        InvoiceIssued(
            tenant_id=invoice.tenant_id,
            invoice_id=invoice.id,
            client_id=invoice.client_id,
            legal_entity_id=invoice.legal_entity_id,
            number=invoice.number or "",
            doc_date=when,
        ),
    )
    return await invoices.detail(ctx, db, "invoice", invoice.id)


async def issue_batch(
    ctx: RequestContext, db: AsyncSession, invoice_ids: list[uuid.UUID], doc_date: date | None = None
) -> IssueBatchOut:
    """One savepoint per document (a failure leaves the others issued); ids in a stable order."""
    results: list[IssueResult] = []
    for inv_id in sorted(set(invoice_ids), key=lambda i: str(i)):
        try:
            async with db.begin_nested():
                out = await issue_invoice(ctx, db, inv_id, doc_date)
            results.append(IssueResult(id=inv_id, ok=True, number=out.number))
        except DomainError as exc:
            results.append(IssueResult(id=inv_id, ok=False, code=exc.code, message=str(exc)))
    ok = sum(1 for r in results if r.ok)
    return IssueBatchOut(issued=ok, failed=len(results) - ok, results=results)


__all__ = ["MODELS", "select"]
