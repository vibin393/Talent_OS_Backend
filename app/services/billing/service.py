# ruff: file-ignore[line-too-long, unused-function-argument]
"""Billing contract (Epic 29): settlement facts and receivable documents, plus the public helpers Epic 30 / 35 use.

Epic 13 contract layer (AD-36): ``lock_for_settlement`` / ``get_receivable_document`` keep their FINAL signatures
(``tests/contracts/signatures.json``). Extra public helpers (reconciliation note 3): ``get_credit_note``,
``register_settlement_probe`` (the IRN-cancel guard asks every registered probe) and ``import_opening_invoice``.
"""

import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, fiscal
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.money import assert_document_currency
from app.models.billing import CreditNote, DebitNote, Invoice
from app.schemas.billing import (
    CreditNoteFacts,
    InvoiceSummary,
    OpeningInvoiceRow,
    ReceivableDocumentSummary,
    SettlementFacts,
)
from app.services.billing.common import conflict, invalid, tenant_of

SettlementProbe = Callable[[RequestContext, AsyncSession, str, uuid.UUID], Awaitable[bool]]
_probes: list[SettlementProbe] = []


def register_settlement_probe(fn: SettlementProbe) -> None:
    """Epic 30 registers ``fn(ctx, db, document_kind, document_id) -> has settlements``."""
    if fn not in _probes:
        _probes.append(fn)


async def has_settlements(
    ctx: RequestContext, db: AsyncSession, document_kind: str, document_id: uuid.UUID
) -> bool:
    for probe in list(_probes):
        if await probe(ctx, db, document_kind, document_id):
            return True
    return False


def _model(document_kind: str) -> type[Invoice] | type[DebitNote]:
    if document_kind == "invoice":
        return Invoice
    if document_kind == "debit_note":
        return DebitNote
    msg = f"unknown receivable document kind {document_kind!r}"
    raise ValueError(msg)


async def _credit_noted(db: AsyncSession, document_kind: str, document_id: uuid.UUID) -> Decimal:
    if document_kind != "invoice":
        return Decimal("0.00")
    total = (
        await db.execute(
            select(func.coalesce(func.sum(CreditNote.document_total), 0)).where(
                CreditNote.original_invoice_id == document_id, CreditNote.status == "issued"
            )
        )
    ).scalar()
    return Decimal(total or 0)


async def lock_for_settlement(
    ctx: RequestContext,
    db: AsyncSession,
    document_kind: Literal["invoice", "debit_note"],
    document_id: uuid.UUID,
) -> SettlementFacts:
    model = _model(document_kind)
    doc = (
        await db.execute(select(model).where(model.id == document_id).with_for_update())
    ).scalar_one_or_none()
    if doc is None or doc.status not in ("issued", "cancelled") or doc.doc_date is None:
        raise NotFound("Receivable document not found")
    return SettlementFacts(
        document_kind=document_kind,
        document_id=doc.id,
        client_id=doc.client_id,
        legal_entity_id=doc.legal_entity_id,
        currency=doc.currency,
        document_total=doc.document_total,
        doc_date=doc.doc_date,
        due_date=doc.due_date,
        status=doc.status,
        fx_rate=doc.fx_rate,
        credit_noted_total=await _credit_noted(db, document_kind, doc.id),
        inr_document_total=doc.inr_document_total,
        taxable_excl_gst=doc.taxable_total,
    )


async def get_receivable_document(
    ctx: RequestContext,
    db: AsyncSession,
    document_kind: Literal["invoice", "debit_note"],
    document_id: uuid.UUID,
) -> ReceivableDocumentSummary:
    model = _model(document_kind)
    doc = (await db.execute(select(model).where(model.id == document_id))).scalar_one_or_none()
    if doc is None or doc.number is None or doc.doc_date is None:
        raise NotFound("Receivable document not found")
    return ReceivableDocumentSummary(
        document_kind=document_kind,
        document_id=doc.id,
        number=doc.number,
        client_id=doc.client_id,
        legal_entity_id=doc.legal_entity_id,
        currency=doc.currency,
        document_total=doc.document_total,
        doc_date=doc.doc_date,
        due_date=doc.due_date,
        status=doc.status,
        fx_rate=doc.fx_rate,
        inr_document_total=doc.inr_document_total,
        taxable_excl_gst=doc.taxable_total,
    )


async def get_credit_note(
    ctx: RequestContext, db: AsyncSession, credit_note_id: uuid.UUID
) -> CreditNoteFacts:
    cn = (await db.execute(select(CreditNote).where(CreditNote.id == credit_note_id))).scalar_one_or_none()
    if cn is None:
        raise NotFound("Credit note not found")
    return CreditNoteFacts(
        id=cn.id,
        invoice_id=cn.original_invoice_id,
        client_id=cn.client_id,
        legal_entity_id=cn.legal_entity_id,
        currency=cn.currency,
        document_total=cn.document_total,
        doc_date=cn.doc_date,
        status=cn.status,
        fx_rate=cn.fx_rate,
        number=cn.number,
    )


async def import_opening_invoice(
    ctx: RequestContext,
    db: AsyncSession,
    row: OpeningInvoiceRow,
    batch_id: uuid.UUID,
    *,
    validate_only: bool = False,
) -> InvoiceSummary:
    """Epic 35 go-live: an issued, imported invoice (no lines, no postings); the series counter is raised under
    its lock so the next issued number follows the legacy one."""
    from app.services import legal_entities_service

    assert_document_currency(row.currency)
    if row.document_total < 0 or row.taxable_total < 0:
        raise invalid("negative_line", "Opening invoice amounts cannot be negative")
    dup = (
        await db.execute(
            select(Invoice.id).where(
                Invoice.number == row.number, Invoice.legal_entity_id == row.legal_entity_id
            )
        )
    ).first()
    if dup is not None:
        raise conflict(
            "invoice_number_taken", "An invoice with this number already exists", number=row.number
        )
    reg = row.gst_registration_id
    if reg is None:
        reg = await legal_entities_service.default_registration_for(ctx, db, row.legal_entity_id, None)
    series_id = await legal_entities_service.series_for(
        ctx, db, legal_entity_id=row.legal_entity_id, gst_registration_id=reg, doc_type="invoice"
    )
    fy = fiscal.fiscal_year(row.doc_date)
    if validate_only:
        return InvoiceSummary(
            id=uuid.uuid4(),
            number=row.number,
            status="issued",
            document_total=row.document_total,
            currency=row.currency,
            validated_only=True,
        )
    inv = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        legal_entity_id=row.legal_entity_id,
        supplier_gst_registration_id=reg,
        client_id=row.client_id,
        series_id=series_id,
        fiscal_year=fy,
        number=row.number,
        doc_date=row.doc_date,
        due_date=row.due_date,
        status="draft",
        currency=row.currency.upper(),
        taxable_total=row.taxable_total,
        charged_tax_total=row.tax_total,
        document_total=row.document_total,
        fx_rate=row.fx_rate,
        inr_document_total=row.document_total
        if row.currency.upper() == "INR"
        else (row.document_total * (row.fx_rate or Decimal(1))).quantize(Decimal("0.01")),
        is_imported=True,
        notes=f"Opening balance import {batch_id}",
    )
    db.add(inv)
    await db.flush()
    inv.status = "issued"
    from app.services.billing.common import now

    inv.issued_at = now()
    await db.flush()
    if row.sequence is not None:
        await legal_entities_service.raise_counter_to(db, series_id, fy, int(row.sequence))
    audit.record(
        "invoice.opening_imported", "invoice", inv.id, session=db, batch_id=str(batch_id), number=row.number
    )
    return InvoiceSummary(
        id=inv.id,
        number=row.number,
        status="issued",
        document_total=row.document_total,
        currency=inv.currency,
    )
