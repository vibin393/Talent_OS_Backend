# ruff: noqa: E501
"""Invoice / note PDFs (FR-149, Rule 46 of the CGST Rules) rendered through ``documents.render`` (template
``invoice.v1`` / ``credit_note.v1`` / ``debit_note.v1``). Rendering is deterministic for a document version and
never changes the document (only ``pdf_render_version`` moves on IRN capture)."""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import DomainError
from app.services import legal_entities_service
from app.services.billing import words
from app.services.billing.common import get_doc, lines_of, tax_lines_of

TEMPLATE = {"invoice": "invoice.v1", "credit_note": "credit_note.v1", "debit_note": "debit_note.v1"}
TITLE = {"invoice": "Tax Invoice", "credit_note": "Credit Note", "debit_note": "Debit Note"}


def _m(value: Any) -> str:
    return words.indian_grouping(Decimal(value).quantize(Decimal("0.01")))


def _addr(address: Any) -> str:
    if not isinstance(address, dict):
        return ""
    parts = [
        address.get(k)
        for k in ("line1", "line2", "address_line1", "address_line2", "city", "state", "postal_code", "pin")
    ]
    return ", ".join(str(p) for p in parts if p)


async def _state_names(ctx: RequestContext, db: AsyncSession) -> dict[str, str]:
    try:
        return {s.code: s.name for s in await legal_entities_service.list_gst_states(ctx, db)}
    except DomainError:
        return {}


async def markdown(ctx: RequestContext, db: AsyncSession, kind: str, doc_id: uuid.UUID) -> tuple[str, Any]:
    doc = await get_doc(db, kind, doc_id)
    lines = await lines_of(db, kind, doc_id)
    taxes = await tax_lines_of(db, kind, doc_id)
    states = await _state_names(ctx, db)
    sup = dict(doc.supplier_snapshot or {})
    rec = dict(doc.recipient_snapshot or {})
    title = TITLE[kind]
    status = "" if doc.status == "issued" else f" ({doc.status.replace('_', ' ').upper()})"
    out = [f"# {title}{status}", ""]
    out.append(f"**{sup.get('legal_name') or sup.get('name') or ''}**  ")
    if sup.get("address") or sup.get("registration_address"):
        out.append(f"{_addr(sup.get('registration_address') or sup.get('address'))}  ")
    out.append(
        f"GSTIN: {sup.get('gstin') or 'Not registered'} | PAN: {sup.get('pan') or '-'} | State: {states.get(sup.get('state_code') or '', '')} ({sup.get('state_code') or '-'})"
    )
    out.append("")
    out.append(
        f"**{title} No:** {doc.number or 'DRAFT (not numbered)'} | **Date:** {doc.doc_date or doc.preview_date or '-'}"
        + (f" | **Due:** {doc.due_date}" if doc.due_date else "")
    )
    if kind != "invoice":
        original = await get_doc(db, "invoice", doc.original_invoice_id)
        out.append(
            f"**Against invoice:** {original.number} dated {original.doc_date} | **Reason:** {getattr(doc, 'reason', '')}"
        )
    out.append("")
    out.append("## Bill to")
    out.append(f"**{rec.get('name') or ''}**  ")
    out.append(
        f"GSTIN: {rec.get('gstin') or 'Unregistered'} | State: {states.get(rec.get('state_code') or '', '')} ({rec.get('state_code') or '-'}) | Country: {rec.get('country') or 'IN'}"
    )
    out.append(
        f"Place of supply: {states.get(doc.pos_state_code or '', 'Outside India' if doc.pos_state_code == '96' else '')} ({doc.pos_state_code or '-'})"
    )
    out.append(f"Tax payable on reverse charge: {'Yes' if doc.reverse_charge else 'No'}")
    if doc.lut_ref:
        out.append(f"Supply meant for export / SEZ under LUT {doc.lut_ref} without payment of IGST")
    po_numbers = sorted({str(ln.po_id) for ln in lines if ln.po_id})
    if po_numbers:
        out.append(f"PO reference(s): {', '.join(po_numbers)}")
    out.append("")
    out.append("| # | Description | SAC | Qty | Unit | Rate | Taxable | Tax |")
    out.append("|---|---|---|---|---|---|---|---|")
    by_line: dict[uuid.UUID, list[Any]] = {}
    for t in taxes:
        by_line.setdefault(t.line_id, []).append(t)
    for ln in lines:
        tax_text = ", ".join(
            f"{t.component.upper()} {Decimal(t.rate_pct).normalize()}% {_m(t.amount)}"
            for t in by_line.get(ln.id, [])
        )
        if ln.outside_taxable_value:
            tax_text = "Pure agent (outside taxable value)"
        out.append(
            f"| {ln.line_no} | {ln.description} | {ln.sac_code} | {Decimal(ln.quantity)} | {ln.unit} | {Decimal(ln.effective_rate)} | {_m(ln.taxable_value)} | {tax_text or '-'} |"
        )
    out.append("")
    comp_totals: dict[str, Decimal] = {}
    for t in taxes:
        comp_totals[t.component] = comp_totals.get(t.component, Decimal(0)) + Decimal(t.amount)
    out.append("| Summary | Amount |")
    out.append("|---|---|")
    out.append(f"| Taxable value | {doc.currency} {_m(doc.taxable_total)} |")
    if Decimal(doc.outside_taxable_total):
        out.append(f"| Reimbursements (pure agent) | {doc.currency} {_m(doc.outside_taxable_total)} |")
    for comp, amount in comp_totals.items():
        label = f"{comp.upper()}" + (" (payable by recipient, reverse charge)" if doc.reverse_charge else "")
        out.append(f"| {label} | {doc.currency} {_m(amount)} |")
    if Decimal(doc.round_off):
        out.append(f"| Round off | {doc.currency} {_m(doc.round_off)} |")
    out.append(f"| **Total** | **{doc.currency} {_m(doc.document_total)}** |")
    if doc.currency != "INR":
        out.append(
            f"| INR equivalent (rate {doc.fx_rate}, {doc.fx_source}) | INR {_m(doc.inr_document_total)} |"
        )
    out.append("")
    out.append(f"Amount in words: {words.amount_in_words(Decimal(doc.document_total), doc.currency)}")
    out.append("")
    if doc.irn:
        out.append(f"IRN: {doc.irn}  ")
        out.append(f"Ack No: {doc.ack_no} | Ack Date: {doc.ack_at}")
        out.append("")
    try:
        bank = await legal_entities_service.bank_account_for_documents(
            ctx, db, doc.legal_entity_id, doc.currency
        )
        out.append(
            f"Bank: {getattr(bank, 'bank_name', '')} | A/c: {getattr(bank, 'account_number', '')} | IFSC: {getattr(bank, 'ifsc', '') or '-'}"
        )
    except DomainError:
        pass
    out.append("")
    out.append(f"For {sup.get('legal_name') or sup.get('name') or ''}  ")
    out.append(f"Authorised signatory{(': ' + sup['signatory_name']) if sup.get('signatory_name') else ''}")
    out.append("")
    out.append("This is a computer-generated document and does not require a signature.")
    return "\n".join(out), doc


async def render(ctx: RequestContext, db: AsyncSession, kind: str, doc_id: uuid.UUID) -> tuple[bytes, str]:
    from app.services.documents import render as doc_render

    text, doc = await markdown(ctx, db, kind, doc_id)
    flow = doc_render.markdown_flowables(text)
    if doc.signed_qr:
        flow.append(doc_render.qr_flowable(doc.signed_qr[:2000]))
    title = f"{TITLE[kind]} {doc.number or 'draft'}"
    data = doc_render.build(flow, header=title, footer=TEMPLATE[kind], title=title)
    filename = f"{(doc.number or 'draft-' + str(doc.id)[:8]).replace('/', '-')}.pdf"
    return data, filename
