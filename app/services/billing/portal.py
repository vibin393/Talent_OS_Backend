# ruff: file-ignore[line-too-long, unused-function-argument]
"""Delivery and the Client Portal view of billing documents (FR-149).

* ``send_document``: refused while the IRN is pending or before issue (409 ``pending_irn`` / ``invalid_state``);
  emails the PDF through ``core.mailer`` (the configured mailer -- console / outbox outside production) and sets
  ``delivery_status=sent``.
* Client Portal: issued documents whose e-invoice status is not ``pending_irn`` only (org RLS ``client``); never
  exposes manual flags, postings or internal notes. A portal view sets ``delivery_status=viewed``.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.schemas.billing import DocDetailOut, SendIn
from app.schemas.billing_portal import PortalInvoiceDetail, PortalInvoiceSummary
from app.services.billing import invoices, pdf, words
from app.services.billing.common import MODELS, conflict, get_doc, invalid, now

PORTAL_KINDS = ("invoice", "credit_note", "debit_note")


async def send_document(
    ctx: RequestContext, db: AsyncSession, kind: str, doc_id: uuid.UUID, data: SendIn
) -> DocDetailOut:
    from app.core import mailer

    doc = await get_doc(db, kind, doc_id, lock=True)
    if doc.status != "issued":
        raise conflict("invalid_state", "Only issued documents can be sent", status=doc.status)
    if doc.einvoice_status == "pending_irn":
        raise conflict("pending_irn", "Record the IRN before sending this document")
    recipients = [a.strip() for a in data.to if a.strip()] or list(
        (doc.recipient_snapshot or {}).get("contacts") or []
    )
    if not recipients:
        raise invalid("recipients_required", "No billing contact to send to")
    from app.services.billing import dsc  # Epic 51: never deliver an unsigned PDF when DSC is required

    if await dsc.delivery_blocked(db, kind, doc):
        raise dsc.dsc_pending_error()
    body, filename = await dsc.delivered_pdf(ctx, db, kind, doc)
    for to in recipients:
        await mailer.send_email(
            mailer.EmailMessage(
                to=to,
                subject=f"{pdf.TITLE[kind]} {doc.number}",
                text=f"Please find attached {pdf.TITLE[kind].lower()} {doc.number} for {doc.currency} {words.indian_grouping(Decimal(doc.document_total))}.",
                tenant_id=doc.tenant_id,
                kind=f"billing.{kind}",
                attachments=(
                    mailer.EmailAttachment(filename=filename, content_type="application/pdf", data=body),
                ),
            )
        )
    doc.delivery_status = "sent"
    doc.last_sent_at = now()
    await db.flush()
    audit.record(f"{kind}.sent", kind, doc.id, session=db, recipients=len(recipients))
    return await invoices.detail(ctx, db, kind, doc.id)


def _visible(stmt: Any, model: Any) -> Any:
    return stmt.where(model.status.in_(("issued", "cancelled")), model.einvoice_status != "pending_irn")


async def client_list(ctx: RequestContext, db: AsyncSession) -> list[PortalInvoiceSummary]:
    out: list[PortalInvoiceSummary] = []
    for kind in PORTAL_KINDS:
        model = MODELS[kind][0]
        for d in (
            await db.execute(_visible(select(model), model).order_by(model.doc_date.desc()).limit(500))
        ).scalars():
            out.append(
                PortalInvoiceSummary(
                    id=d.id,
                    kind=kind,
                    number=d.number,
                    doc_date=d.doc_date,
                    due_date=d.due_date,
                    currency=d.currency,
                    document_total=d.document_total,
                    status=d.status,
                    payment_status=None,
                )
            )
    return sorted(out, key=lambda r: (r.doc_date, r.number or ""), reverse=True)


async def _portal_doc(db: AsyncSession, kind: str, doc_id: uuid.UUID) -> Any:
    if kind not in PORTAL_KINDS:
        raise NotFound("Document not found")
    model = MODELS[kind][0]
    doc = (await db.execute(_visible(select(model).where(model.id == doc_id), model))).scalar_one_or_none()
    if doc is None:
        raise NotFound("Document not found")
    return doc


async def client_detail(
    ctx: RequestContext, db: AsyncSession, kind: str, doc_id: uuid.UUID
) -> PortalInvoiceDetail:
    doc = await _portal_doc(db, kind, doc_id)
    full = await invoices.detail(ctx, db, kind, doc.id)
    if doc.delivery_status != "viewed":
        doc.delivery_status = "viewed"
        await db.flush()
    return PortalInvoiceDetail(
        id=doc.id,
        kind=kind,
        number=doc.number,
        doc_date=doc.doc_date,
        due_date=doc.due_date,
        currency=doc.currency,
        status=doc.status,
        payment_status=None,
        taxable_total=doc.taxable_total,
        tax_total=doc.charged_tax_total,
        document_total=doc.document_total,
        amount_in_words=full.amount_in_words,
        irn=doc.irn,
        pdf_available=not _dsc_blocked(kind, doc),
        lines=[
            {
                "line_no": ln.line_no,
                "description": ln.description,
                "sac_code": ln.sac_code,
                "quantity": str(ln.quantity),
                "unit": ln.unit,
                "rate": str(ln.effective_rate),
                "taxable_value": str(ln.taxable_value),
            }
            for ln in full.lines
        ],
    )


async def client_pdf(
    ctx: RequestContext, db: AsyncSession, kind: str, doc_id: uuid.UUID
) -> tuple[bytes, str]:
    doc = await _portal_doc(db, kind, doc_id)
    from app.services.billing import dsc  # Epic 51: the PDF stays hidden until the DSC signature exists

    if await dsc.delivery_blocked(db, kind, doc):
        raise dsc.dsc_pending_error()
    return await dsc.delivered_pdf(ctx, db, kind, doc)


def _dsc_blocked(kind: str, doc: Any) -> bool:
    from app.services.billing import dsc

    return dsc.delivery_blocked_sync(kind, doc)
