# ruff: noqa: E501, ARG001
"""E-invoice manual capture (FR-151, AD-29): no live GSP. Finance downloads the INV-01 JSON of ``pending_irn``
documents, registers them on the IRP portal and records the IRN / Ack / signed QR manually or by importing the
portal's response file (sha256-deduped preview -> apply). Capture only touches allow-listed columns, so the content
hash never changes. IRN cancellation (Invoices only, a person, within 24 h of the Ack, no settlements) cancels the
Invoice, releases its postings and reverses PO consumption; the number stays used.
"""

import csv
import hashlib
import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.events import emit
from app.events.billing import EinvoiceStatusChanged, InvoiceCancelled
from app.models.billing import EinvoiceImport
from app.schemas.billing import (
    DocDetailOut,
    DocSummaryOut,
    EinvoiceApplyOut,
    EinvoiceImportOut,
    EinvoiceImportRow,
    IrnCancelIn,
    IrnIn,
)
from app.services import postings
from app.services.billing import einvoice_schema, invoices, issue
from app.services.billing.common import (
    DOC_KINDS,
    MODELS,
    conflict,
    get_doc,
    invalid,
    lines_of,
    now,
    tax_lines_of,
    tenant_of,
    user_id,
)
from app.services.billing.service import has_settlements

CANCEL_WINDOW = timedelta(hours=24)
_TYPES = {"INV": "invoice", "CRN": "credit_note", "DBN": "debit_note"}


async def pending(
    ctx: RequestContext, db: AsyncSession, gst_registration_id: uuid.UUID | None = None
) -> list[DocSummaryOut]:
    out: list[DocSummaryOut] = []
    for kind in DOC_KINDS:
        model = MODELS[kind][0]
        stmt = select(model).where(model.status == "issued", model.einvoice_status == "pending_irn")
        if gst_registration_id is not None:
            stmt = stmt.where(model.supplier_gst_registration_id == gst_registration_id)
        out += [
            invoices.summary(kind, r) for r in (await db.execute(stmt.order_by(model.issued_at))).scalars()
        ]
    return out


async def payload_for(db: AsyncSession, kind: str, doc: Any) -> dict[str, Any]:
    original = None
    if kind != "invoice":
        original = await get_doc(db, "invoice", doc.original_invoice_id)
    return einvoice_schema.build(
        kind, doc, await lines_of(db, kind, doc.id), await tax_lines_of(db, kind, doc.id), original=original
    )


async def export(
    ctx: RequestContext,
    db: AsyncSession,
    gst_registration_id: uuid.UUID | None,
    doc_ids: list[uuid.UUID] | None,
) -> str:
    wanted = set(doc_ids or [])
    payloads = []
    for summary in await pending(ctx, db, gst_registration_id):
        if wanted and summary.id not in wanted:
            continue
        doc = await get_doc(db, summary.kind, summary.id)
        payloads.append(await payload_for(db, summary.kind, doc))
    audit.record("einvoice.exported", "einvoice", None, session=db, count=len(payloads))
    return einvoice_schema.dumps(payloads)


async def _capture(
    ctx: RequestContext,
    db: AsyncSession,
    kind: str,
    doc: Any,
    irn: str,
    ack_no: str,
    ack_at: datetime,
    signed_qr: str | None,
) -> None:
    if doc.status != "issued":
        raise conflict("invalid_state", "Only issued documents carry an IRN", status=doc.status)
    if doc.einvoice_status == "irn_registered":
        if doc.irn == irn:
            return
        raise conflict("irn_already_registered", "This document already has a different IRN")
    if doc.einvoice_status != "pending_irn":
        raise conflict(
            "einvoice_not_applicable", "This document does not need an IRN", status=doc.einvoice_status
        )
    doc.irn, doc.ack_no, doc.ack_at, doc.signed_qr = irn, ack_no, ack_at, signed_qr
    doc.einvoice_status = "irn_registered"
    doc.pdf_render_version = int(doc.pdf_render_version or 0) + 1
    await db.flush()
    emit(
        db,
        EinvoiceStatusChanged(
            tenant_id=doc.tenant_id, document_kind=kind, document_id=doc.id, status="irn_registered"
        ),
    )
    audit.record("einvoice.irn_recorded", kind, doc.id, session=db, ack_no=ack_no, by=str(user_id(ctx)))


async def record_irn(
    ctx: RequestContext, db: AsyncSession, kind: str, doc_id: uuid.UUID, data: IrnIn
) -> DocDetailOut:
    if kind not in DOC_KINDS:
        raise invalid("unknown_document_kind", "Unknown document kind")
    doc = await get_doc(db, kind, doc_id, lock=True)
    await _capture(ctx, db, kind, doc, data.irn, data.ack_no, data.ack_at, data.signed_qr)
    return await invoices.detail(ctx, db, kind, doc.id)


# ---------------------------------------------------------------- portal response import
def _parse(content: bytes, filename: str) -> list[dict[str, Any]]:
    text = content.decode("utf-8-sig", errors="replace").strip()
    rows: list[dict[str, Any]] = []
    if filename.lower().endswith(".csv") or not text.startswith(("[", "{")):
        for r in csv.DictReader(io.StringIO(text)):
            norm = {k.strip().lower().replace(" ", "_"): (v or "").strip() for k, v in r.items() if k}
            rows.append(
                {
                    "gstin": norm.get("gstin") or norm.get("seller_gstin"),
                    "doc_type": norm.get("doc_type") or norm.get("type") or "INV",
                    "number": norm.get("doc_no") or norm.get("number") or norm.get("document_number"),
                    "irn": norm.get("irn"),
                    "ack_no": norm.get("ack_no"),
                    "ack_at": norm.get("ack_date") or norm.get("ack_dt") or norm.get("ack_at"),
                    "signed_qr": norm.get("signed_qr") or norm.get("signed_qr_code"),
                }
            )
        return rows
    data = json.loads(text)
    items = data if isinstance(data, list) else data.get("data") or data.get("results") or [data]
    for it in items:
        if not isinstance(it, dict):
            continue
        doc = it.get("DocDtls") or {}
        rows.append(
            {
                "gstin": it.get("SellerGstin")
                or (it.get("SellerDtls") or {}).get("Gstin")
                or it.get("gstin"),
                "doc_type": doc.get("Typ") or it.get("DocTyp") or it.get("doc_type") or "INV",
                "number": doc.get("No") or it.get("DocNo") or it.get("number"),
                "irn": it.get("Irn") or it.get("irn"),
                "ack_no": str(it.get("AckNo") or it.get("ack_no") or "") or None,
                "ack_at": it.get("AckDt") or it.get("ack_at"),
                "signed_qr": it.get("SignedQRCode") or it.get("signed_qr"),
            }
        )
    return rows


def _parse_ack(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(value.strip()[:19], fmt)
            return parsed.replace(tzinfo=UTC) - timedelta(hours=5, minutes=30)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


async def _match(db: AsyncSession, row: dict[str, Any]) -> EinvoiceImportRow:
    base = {k: row.get(k) for k in ("gstin", "doc_type", "number", "irn", "ack_no", "ack_at", "signed_qr")}
    kind = _TYPES.get(str(row.get("doc_type") or "INV").upper())
    irn = str(row.get("irn") or "").strip().lower()
    if (
        kind is None
        or not row.get("number")
        or len(irn) != 64
        or any(c not in "0123456789abcdef" for c in irn)
        or _parse_ack(row.get("ack_at")) is None
        or not row.get("ack_no")
    ):
        return EinvoiceImportRow(
            **base, status="invalid", message="Missing or invalid document type, number, IRN or Ack"
        )
    base["irn"] = irn
    model = MODELS[kind][0]
    docs = list(
        (
            await db.execute(select(model).where(model.number == row["number"], model.status == "issued"))
        ).scalars()
    )
    gstin = (row.get("gstin") or "").upper()
    if gstin:
        docs = [d for d in docs if (d.supplier_snapshot or {}).get("gstin", "").upper() == gstin]
    if not docs:
        return EinvoiceImportRow(**base, status="unmatched", document_kind=kind)
    if len(docs) > 1:
        return EinvoiceImportRow(
            **base, status="conflict", document_kind=kind, message="Several documents match"
        )
    doc = docs[0]
    if doc.einvoice_status == "irn_registered":
        status = "already_registered" if doc.irn == irn else "conflict"
        return EinvoiceImportRow(**base, status=status, document_kind=kind, document_id=doc.id)
    if doc.einvoice_status != "pending_irn":
        return EinvoiceImportRow(
            **base, status="conflict", document_kind=kind, document_id=doc.id, message="Not pending IRN"
        )
    return EinvoiceImportRow(**base, status="matched", document_kind=kind, document_id=doc.id)


async def import_preview(
    ctx: RequestContext, db: AsyncSession, content: bytes, filename: str
) -> EinvoiceImportOut:
    digest = hashlib.sha256(content).hexdigest()
    applied = (
        await db.execute(
            select(EinvoiceImport.id).where(
                EinvoiceImport.sha256 == digest, EinvoiceImport.status == "applied"
            )
        )
    ).scalar_one_or_none()
    try:
        parsed = _parse(content, filename)
    except (ValueError, UnicodeDecodeError) as exc:
        raise invalid("import_unreadable", "The file is not a readable JSON or CSV response") from exc
    rows = [await _match(db, r) for r in parsed]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    batch = EinvoiceImport(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        sha256=digest,
        status="previewed",
        rows=[r.model_dump(mode="json") for r in rows],
        summary={"counts": counts, "filename": filename[:200]},
        created_by=user_id(ctx),
    )
    db.add(batch)
    await db.flush()
    return EinvoiceImportOut(
        batch_id=batch.id, status="previewed", duplicate_of=applied, counts=counts, rows=rows
    )


async def import_apply(ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID) -> EinvoiceApplyOut:
    batch = (
        await db.execute(select(EinvoiceImport).where(EinvoiceImport.id == batch_id).with_for_update())
    ).scalar_one_or_none()
    if batch is None:
        from app.services.billing.common import not_found

        raise not_found("import batch")
    if batch.status == "applied":
        return EinvoiceApplyOut(batch_id=batch.id, applied=int((batch.summary or {}).get("applied", 0)))
    done = (
        await db.execute(
            select(EinvoiceImport.id).where(
                EinvoiceImport.sha256 == batch.sha256, EinvoiceImport.status == "applied"
            )
        )
    ).scalar_one_or_none()
    if done is not None:  # the same file was applied already: no-op
        batch.status = "discarded"
        await db.flush()
        return EinvoiceApplyOut(batch_id=batch.id, applied=0)
    applied = 0
    for raw in batch.rows or []:
        row = EinvoiceImportRow.model_validate(raw)
        if row.status != "matched" or row.document_id is None or row.document_kind is None:
            continue
        doc = await get_doc(db, row.document_kind, row.document_id, lock=True)
        if doc.einvoice_status != "pending_irn":
            continue
        await _capture(
            ctx,
            db,
            row.document_kind,
            doc,
            row.irn or "",
            row.ack_no or "",
            _parse_ack(row.ack_at) or now(),
            row.signed_qr,
        )
        applied += 1
    batch.status = "applied"
    batch.summary = {**(batch.summary or {}), "applied": applied}
    await db.flush()
    return EinvoiceApplyOut(batch_id=batch.id, applied=applied)


# ---------------------------------------------------------------- IRN cancellation
async def cancel_irn(
    ctx: RequestContext, db: AsyncSession, invoice_id: uuid.UUID, data: IrnCancelIn
) -> DocDetailOut:
    require_human(actor_from_ctx(ctx))
    invoice = await get_doc(db, "invoice", invoice_id, lock=True)
    if invoice.status == "cancelled" and invoice.cancel_basis == "irn_cancelled":
        return await invoices.detail(ctx, db, "invoice", invoice.id)
    if invoice.status != "issued" or invoice.einvoice_status != "irn_registered" or invoice.ack_at is None:
        raise conflict(
            "invalid_state", "Only an issued invoice with a registered IRN can be cancelled on the IRP"
        )
    if now() > invoice.ack_at + CANCEL_WINDOW:
        raise conflict(
            "irn_cancel_window_closed", "IRNs can only be cancelled within 24 hours of acknowledgement"
        )
    if await has_settlements(ctx, db, "invoice", invoice.id):
        raise conflict("invoice_has_settlements", "Payments or credits are applied to this invoice")
    rows = await lines_of(db, "invoice", invoice.id)
    invoice.status = "cancelled"
    invoice.cancel_basis = "irn_cancelled"
    invoice.cancelled_at = now()
    invoice.einvoice_status = "irn_cancelled"
    invoice.irn_cancelled_at = now()
    invoice.irn_cancel_reason = data.reason
    await db.flush()
    await postings.release(ctx, db, "billing", reason="irn_cancelled", document_id=invoice.id)
    await _reverse_po(ctx, db, invoice, rows)
    emit(
        db, InvoiceCancelled(tenant_id=invoice.tenant_id, invoice_id=invoice.id, cancel_basis="irn_cancelled")
    )
    emit(
        db,
        EinvoiceStatusChanged(
            tenant_id=invoice.tenant_id,
            document_kind="invoice",
            document_id=invoice.id,
            status="irn_cancelled",
        ),
    )
    audit.record("einvoice.irn_cancelled", "invoice", invoice.id, session=db, reason=data.reason)
    return await invoices.detail(ctx, db, "invoice", invoice.id)


async def _reverse_po(ctx: RequestContext, db: AsyncSession, invoice: Any, rows: list[Any]) -> None:
    """Negative PO consumption for the cancelled invoice (recorded against the invoice id as a credit)."""
    await issue._po_consumption(ctx, db, "credit_note", invoice, rows, -1)
