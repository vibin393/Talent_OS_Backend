# ruff: file-ignore[line-too-long]
"""Finance document -> ``LedgerDocumentDTO`` (Epic 71, story 71.5, spec A8).

Only ``issued`` invoices / notes are built (``cancelled`` ones only to void what was pushed). The payload is
provider-neutral (adapters translate it):

``number, date, due_date, place_of_supply, place_of_supply_name, supply_type, intra_state, reverse_charge, lut_ref,
gst_treatment, gst_no, currency, exchange_rate, irn, ack_no, reference_number, total, taxable_total, tax_total,
round_off, inr_total, inr_round_off, round_off_ref, lines[{description, sac, quantity, rate, taxable_value,
inr_taxable_value, discount, revenue_ref, item_ref, tax_ref, taxes[{component, rate, amount, inr_amount, ledger_ref}]}],
linked_invoice_ref, linked_invoice_number, reason, action (create | update | void), external_ref`` and, for payments,
``payment{amount, inr_amount, mode, reference, bank_ref, tds_amount, inr_tds_amount, tds_ref, allocations[{invoice_id,
invoice_number, invoice_ref, amount, inr_amount, tds, inr_tds}]}``.

A missing mapping (revenue for a category, the GST ledger / tax for a rate, the customer for Tally, the bank / TDS
ledger for a Tally receipt) is reported as ``(kind, key)`` instead of a DTO.
"""

import dataclasses
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.integrations import find_manifest
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
from app.models.clients import Client
from app.models.receivables import Allocation, Payment, TdsDeduction
from app.models.reference import GstState
from app.services import integrations_service
from app.services.connectors.accounting.port import LedgerDocumentDTO
from app.services.ledger_sync.mappings import MappingSet, rate_key

__all__ = ["DOC_MODELS", "Built", "build", "doc_version", "load_document"]

DOC_MODELS: dict[str, tuple[Any, Any, Any, str]] = {
    "invoice": (Invoice, InvoiceLine, InvoiceTaxLine, "invoice_id"),
    "credit_note": (CreditNote, CreditNoteLine, CreditNoteTaxLine, "credit_note_id"),
    "debit_note": (DebitNote, DebitNoteLine, DebitNoteTaxLine, "debit_note_id"),
}


@dataclasses.dataclass
class Built:
    dto: LedgerDocumentDTO | None
    missing: list[tuple[str, str]]
    master: dict[str, Any] | None = None  # Tally party ledger master (include_masters)


async def load_document(db: AsyncSession, kind: str, document_id: uuid.UUID) -> Any:
    if kind == "payment":
        return await db.get(Payment, document_id)
    model = DOC_MODELS[kind][0]
    return await db.get(model, document_id)


def doc_version(kind: str, doc: Any) -> int:
    """1 for the issued document; 2 once cancelled (invoice / note) or its IRN cancelled; payments 1 (2 reversed)."""
    if kind == "payment":
        return 2 if getattr(doc, "reversed_at", None) else 1
    return 2 if doc.status == "cancelled" or getattr(doc, "irn_cancelled_at", None) else 1


def doc_date(kind: str, doc: Any) -> date:
    return doc.payment_date if kind == "payment" else (doc.doc_date or date.today())


def _num(v: Any) -> str:
    return format(Decimal(str(v or 0)).quantize(Decimal("0.01")), "f")


def _customer_key(doc: Any) -> tuple[str, str]:
    profile = getattr(doc, "billing_profile_id", None)
    return (f"{doc.client_id}:{profile}" if profile else str(doc.client_id)), str(doc.client_id)


async def _customer(db: AsyncSession, installation_id: uuid.UUID, doc: Any, ms: MappingSet) -> str | None:
    full, plain = _customer_key(doc)
    ref = ms.get("customer", full, wildcard=False) or ms.get("customer", plain, wildcard=False)
    if ref:
        return ref
    link = await integrations_service.find_link(db, installation_id, "client", local_id=doc.client_id)
    return link.external_id if link is not None else None


def _gst_treatment(snap: dict[str, Any], doc: Any) -> str:
    if (getattr(doc, "supply_type", None) or "").startswith("exp") or (snap.get("country") and str(snap.get("country"))[:2].upper() != "IN"):
        return "overseas"
    return "business_gst" if snap.get("gstin") else "business_none"


async def build(
    db: AsyncSession,
    installation_id: uuid.UUID,
    kind: str,
    doc: Any,
    ms: MappingSet,
    *,
    version: int,
    action: str = "create",
    external_ref: str | None = None,
    include_masters: bool = False,
) -> Built:
    inst_key = await _connector_key(db, installation_id)
    tally = inst_key == "tally_xml"
    if kind == "payment":
        return await _build_payment(db, installation_id, doc, ms, version, tally)
    _model, line_model, tax_model, parent_col = DOC_MODELS[kind]
    lines = list((await db.execute(select(line_model).where(getattr(line_model, parent_col) == doc.id).order_by(line_model.line_no))).scalars())
    taxes = list((await db.execute(select(tax_model).where(getattr(tax_model, parent_col) == doc.id))).scalars())
    by_line: dict[uuid.UUID, list[Any]] = {}
    for t in taxes:
        by_line.setdefault(t.line_id, []).append(t)
    snap = dict(doc.recipient_snapshot or {})
    missing: list[tuple[str, str]] = []
    customer = await _customer(db, installation_id, doc, ms)
    master = None
    if customer is None and tally:
        client = await db.get(Client, doc.client_id)
        name = str(snap.get("legal_name") or snap.get("name") or (client.name if client else "") or "")
        if include_masters and name:
            customer = name
            state = await db.get(GstState, snap.get("state_code")) if snap.get("state_code") else None
            master = {"name": name, "gstin": snap.get("gstin"), "state": state.name if state else None, "country": "India" if not snap.get("country") or str(snap.get("country")).upper().startswith("IN") else str(snap.get("country"))}
        else:
            missing.append(("customer", _customer_key(doc)[1]))
    out_lines = []
    for line in lines:
        cat = line.category or "*"
        revenue = ms.get("revenue", cat)
        if revenue is None:
            missing.append(("revenue", cat))
        charged = [t for t in by_line.get(line.id, []) if t.charged and Decimal(str(t.amount)) > 0]
        tax_entries = []
        for t in charged:
            key = rate_key(t.component, t.rate_pct)
            ledger = ms.get("gst_output", key, wildcard=False)
            if ledger is None and tally:
                missing.append(("gst_output", key))
            tax_entries.append({"component": t.component, "rate": _num(t.rate_pct), "amount": _num(t.amount), "inr_amount": _num(t.inr_amount or t.amount), "ledger_ref": ledger})
        tax_ref = None
        if charged and not tally:
            primary = next((t for t in charged if t.component in ("igst", "cgst")), charged[0])
            combined = sum((Decimal(str(t.rate_pct)) for t in charged if t.component in ("cgst", "sgst", "igst")), Decimal(0))
            key = rate_key(primary.component, primary.rate_pct)
            tax_ref = ms.get("gst_output", key, wildcard=False) or ms.get("gst_output", rate_key("gst", combined), wildcard=False)
            if tax_ref is None:
                missing.append(("gst_output", key))
        out_lines.append({
            "description": line.description, "sac": line.sac_code, "quantity": _num(line.quantity), "rate": format(Decimal(str(line.effective_rate or line.rate)), "f"),
            "taxable_value": _num(line.taxable_value), "inr_taxable_value": _num(line.inr_taxable_value or line.taxable_value),
            "discount": _num(line.discount_amount) if line.discount_amount else None, "revenue_ref": revenue,
            "item_ref": ms.get("item", line.sac_code), "tax_ref": tax_ref, "taxes": tax_entries,
        })  # fmt: skip
    round_off = Decimal(str(doc.round_off or 0))
    round_ref = ms.get("round_off", "*")
    if round_off and tally and round_ref is None:
        missing.append(("round_off", "*"))
    state = await db.get(GstState, doc.pos_state_code) if doc.pos_state_code else None
    payload: dict[str, Any] = {
        "action": action, "external_ref": external_ref, "due_date": doc.due_date.isoformat() if getattr(doc, "due_date", None) else None,
        "place_of_supply": doc.pos_state_code, "place_of_supply_name": state.name if state else None, "supply_type": doc.supply_type,
        "intra_state": bool(doc.intra_state), "reverse_charge": bool(doc.reverse_charge), "lut_ref": doc.lut_ref,
        "gst_treatment": _gst_treatment(snap, doc), "gst_no": snap.get("gstin"), "currency": doc.currency,
        "exchange_rate": _num(doc.fx_rate) if doc.fx_rate else None, "irn": doc.irn, "ack_no": doc.ack_no,
        "reference_number": snap.get("po_number"), "total": _num(doc.document_total), "taxable_total": _num(doc.taxable_total),
        "tax_total": _num(doc.charged_tax_total), "round_off": _num(round_off), "inr_total": _num(doc.inr_document_total or doc.document_total),
        "inr_round_off": _num(round_off if doc.currency == "INR" else (round_off * Decimal(str(doc.fx_rate or 1)))), "round_off_ref": round_ref,
        "lines": out_lines, "reason": getattr(doc, "reason", None),
    }  # fmt: skip
    if kind in ("credit_note", "debit_note"):
        original = await db.get(Invoice, doc.original_invoice_id)
        payload["linked_invoice_number"] = original.number if original else None
        link = await integrations_service.find_link(db, installation_id, "invoice", local_id=doc.original_invoice_id)
        payload["linked_invoice_ref"] = link.external_id if link is not None else None
    if missing:
        return Built(None, sorted(set(missing)), master)
    dto = LedgerDocumentDTO(
        document_id=doc.id, document_version=version, kind=kind, number=str(doc.number or ""), issue_date=doc.doc_date,  # type: ignore[arg-type]
        legal_entity_id=doc.legal_entity_id, customer_ref=customer, payload=payload,
    )
    return Built(dto, [], master)


async def _connector_key(db: AsyncSession, installation_id: uuid.UUID) -> str | None:
    from app.models.oauth import AppInstallation

    inst = await db.get(AppInstallation, installation_id)
    m = find_manifest(inst.connector_key) if inst is not None else None
    return m.key if m is not None else None


async def _build_payment(db: AsyncSession, installation_id: uuid.UUID, pay: Payment, ms: MappingSet, version: int, tally: bool) -> Built:
    missing: list[tuple[str, str]] = []
    customer = ms.get("customer", str(pay.client_id), wildcard=False)
    if customer is None:
        link = await integrations_service.find_link(db, installation_id, "client", local_id=pay.client_id)
        customer = link.external_id if link is not None else None
    if customer is None:
        missing.append(("customer", str(pay.client_id)))
    bank = ms.get("bank_account", str(pay.receiving_bank_account_id) if pay.receiving_bank_account_id else None)
    if bank is None and tally:
        missing.append(("bank_account", str(pay.receiving_bank_account_id or "*")))
    allocs = list((await db.execute(select(Allocation).where(Allocation.payment_id == pay.id, Allocation.invoice_id.is_not(None)))).scalars())
    tds_rows = list((await db.execute(select(TdsDeduction).where(TdsDeduction.payment_id == pay.id))).scalars())
    tds_by_inv: dict[uuid.UUID, Decimal] = {}
    for t in tds_rows:
        if t.invoice_id:
            tds_by_inv[t.invoice_id] = tds_by_inv.get(t.invoice_id, Decimal(0)) + Decimal(str(t.amount))
    tds_total = sum((Decimal(str(t.amount)) for t in tds_rows), Decimal(0))
    tds_ref = ms.get("tds_receivable", "*")
    if tds_total and tally and tds_ref is None:
        missing.append(("tds_receivable", "*"))
    allocations = []
    for a in allocs:
        inv = await db.get(Invoice, a.invoice_id)
        link = await integrations_service.find_link(db, installation_id, "invoice", local_id=a.invoice_id)
        if link is None and not tally:
            continue  # the invoice is not in the ledger: the payment is applied on account
        allocations.append({
            "invoice_id": str(a.invoice_id), "invoice_number": inv.number if inv else None, "invoice_ref": link.external_id if link else None,
            "amount": _num(a.amount), "inr_amount": _num(a.inr_at_payment or a.amount), "tds": _num(tds_by_inv.get(a.invoice_id, 0)),
            "inr_tds": _num(tds_by_inv.get(a.invoice_id, 0)),
        })  # fmt: skip
    if missing:
        return Built(None, sorted(set(missing)))
    payload = {
        "action": "create",
        "currency": pay.currency,
        "payment": {
            "amount": _num(pay.amount), "inr_amount": _num(pay.inr_amount), "mode": pay.mode, "reference": pay.reference,
            "bank_ref": bank, "tds_amount": _num(tds_total), "inr_tds_amount": _num(tds_total), "tds_ref": tds_ref, "allocations": allocations,
        },
    }  # fmt: skip
    dto = LedgerDocumentDTO(
        document_id=pay.id, document_version=version, kind="payment", number=pay.human_id, issue_date=pay.payment_date,
        legal_entity_id=pay.legal_entity_id, customer_ref=customer, payload=payload,
    )
    return Built(dto, [])
