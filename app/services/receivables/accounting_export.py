# ruff: file-ignore[line-too-long]
"""Accounting export (FR-160, AD-29): a generic ledger (CSV / XLSX) of Invoices, Credit Notes, Debit Notes (tax and
INR), payments, allocations with realized FX, TDS / GST-TDS, write-offs and reversals in a date range, each with
its ``prior_period`` flag. Tenant mapping templates choose columns and map row kinds to ledger accounts. Rows of
an overlapping earlier export are flagged ``previously_exported_in``. Prebuilt Tally / Zoho formats wait for OQ6.
"""

import csv
import io
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.billing import CreditNote, DebitNote, Invoice
from app.models.receivables import (
    AccountingExport,
    AccountingExportItem,
    AccountingExportTemplate,
    Allocation,
    AllocationReversal,
    Payment,
    TdsDeduction,
    TdsDeductionReversal,
    WriteOff,
    WriteOffReversal,
)
from app.schemas.receivables import ExportIn, ExportOut, ExportTemplateIn, ExportTemplateOut
from app.services.receivables.common import invalid, target_of, tenant_of, user_id

DEFAULT_COLUMNS = ["date", "row_kind", "reference", "document_number", "client_id", "currency", "amount", "inr_amount", "tax_amount", "realized_fx", "account", "prior_period", "previously_exported_in"]


async def list_templates(ctx: RequestContext, db: AsyncSession) -> list[ExportTemplateOut]:  # ruff: ignore[unused-function-argument]
    rows = (await db.execute(select(AccountingExportTemplate).where(AccountingExportTemplate.active.is_(True)).order_by(AccountingExportTemplate.name))).scalars()
    return [ExportTemplateOut.model_validate(r) for r in rows]


async def create_template(ctx: RequestContext, db: AsyncSession, data: ExportTemplateIn) -> ExportTemplateOut:  # ruff: ignore[unused-function-argument]
    unknown = [c for c in data.columns if c not in DEFAULT_COLUMNS]
    if unknown:
        raise invalid("unknown_columns", "Unknown export columns", columns=unknown)
    row = AccountingExportTemplate(id=uuid.uuid4(), tenant_id=tenant_of(db), name=data.name, columns=data.columns or DEFAULT_COLUMNS, account_map=data.account_map)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return ExportTemplateOut.model_validate(row)


def _d(v: Any) -> str:
    return str(Decimal(v).quantize(Decimal("0.01"))) if v is not None else ""


async def collect(db: AsyncSession, legal_entity_id: uuid.UUID, start: date, end: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    numbers: dict[uuid.UUID, str | None] = {}
    for kind, m in (("invoice", Invoice), ("credit_note", CreditNote), ("debit_note", DebitNote)):
        stmt = select(m).where(m.legal_entity_id == legal_entity_id, m.status.in_(("issued", "cancelled")), m.doc_date >= start, m.doc_date <= end)
        for doc in (await db.execute(stmt)).scalars():
            numbers[doc.id] = doc.number
            rows.append({"date": doc.doc_date, "row_kind": kind, "ref_kind": kind, "ref_id": doc.id, "reference": doc.number, "document_number": doc.number, "client_id": doc.client_id, "currency": doc.currency,
                         "amount": _d(doc.document_total), "inr_amount": _d(doc.inr_document_total), "tax_amount": _d(doc.charged_tax_total), "realized_fx": "", "prior_period": False})  # fmt: skip
    for p in (await db.execute(select(Payment).where(Payment.legal_entity_id == legal_entity_id, Payment.payment_date >= start, Payment.payment_date <= end))).scalars():
        rows.append({"date": p.payment_date, "row_kind": "payment", "ref_kind": "payment", "ref_id": p.id, "reference": p.human_id, "document_number": p.reference, "client_id": p.client_id, "currency": p.currency,
                     "amount": _d(p.amount), "inr_amount": _d(p.inr_amount), "tax_amount": "", "realized_fx": "", "prior_period": p.prior_period})  # fmt: skip

    async def doc_number(row: Any) -> str | None:
        _, doc_id = target_of(row)
        if doc_id not in numbers:
            for m in (Invoice, DebitNote):
                n = (await db.execute(select(m.number).where(m.id == doc_id))).scalar_one_or_none()
                if n is not None:
                    numbers[doc_id] = n
        return numbers.get(doc_id)

    for a in (await db.execute(select(Allocation).where(Allocation.legal_entity_id == legal_entity_id, Allocation.allocation_date >= start, Allocation.allocation_date <= end))).scalars():
        rows.append({"date": a.allocation_date, "row_kind": "allocation", "ref_kind": "allocation", "ref_id": a.id, "reference": str(a.payment_id or a.credit_note_id), "document_number": await doc_number(a), "client_id": a.client_id,
                     "currency": a.currency, "amount": _d(a.amount), "inr_amount": _d(a.inr_at_document), "tax_amount": "", "realized_fx": _d(a.realized_fx), "prior_period": a.prior_period})  # fmt: skip
    rev_stmt = select(AllocationReversal, Allocation).join(Allocation, Allocation.id == AllocationReversal.allocation_id).where(AllocationReversal.legal_entity_id == legal_entity_id, AllocationReversal.reversal_date >= start, AllocationReversal.reversal_date <= end)
    for r, a in (await db.execute(rev_stmt)).all():
        rows.append({"date": r.reversal_date, "row_kind": "allocation_reversal", "ref_kind": "allocation_reversal", "ref_id": r.id, "reference": str(a.id), "document_number": await doc_number(a), "client_id": a.client_id,
                     "currency": a.currency, "amount": _d(r.amount), "inr_amount": _d(r.inr_at_document), "tax_amount": "", "realized_fx": _d(r.realized_fx), "prior_period": r.prior_period})  # fmt: skip
    for t in (await db.execute(select(TdsDeduction).where(TdsDeduction.legal_entity_id == legal_entity_id, TdsDeduction.deduction_date >= start, TdsDeduction.deduction_date <= end))).scalars():
        rows.append({"date": t.deduction_date, "row_kind": t.kind, "ref_kind": "tds", "ref_id": t.id, "reference": t.section_code, "document_number": await doc_number(t), "client_id": t.client_id,
                     "currency": t.currency, "amount": _d(t.amount), "inr_amount": _d(t.inr_amount), "tax_amount": "", "realized_fx": "", "prior_period": t.prior_period})  # fmt: skip
    trev = select(TdsDeductionReversal, TdsDeduction).join(TdsDeduction, TdsDeduction.id == TdsDeductionReversal.tds_deduction_id).where(TdsDeductionReversal.legal_entity_id == legal_entity_id, TdsDeductionReversal.reversal_date >= start, TdsDeductionReversal.reversal_date <= end)
    for r, t in (await db.execute(trev)).all():
        rows.append({"date": r.reversal_date, "row_kind": f"{t.kind}_reversal", "ref_kind": "tds_reversal", "ref_id": r.id, "reference": str(t.id), "document_number": await doc_number(t), "client_id": t.client_id,
                     "currency": t.currency, "amount": _d(-t.amount), "inr_amount": _d(-t.inr_amount), "tax_amount": "", "realized_fx": "", "prior_period": r.prior_period})  # fmt: skip
    for w in (await db.execute(select(WriteOff).where(WriteOff.legal_entity_id == legal_entity_id, WriteOff.write_off_date >= start, WriteOff.write_off_date <= end))).scalars():
        rows.append({"date": w.write_off_date, "row_kind": "write_off", "ref_kind": "write_off", "ref_id": w.id, "reference": str(w.request_id), "document_number": await doc_number(w), "client_id": w.client_id,
                     "currency": w.currency, "amount": _d(w.amount), "inr_amount": _d(w.inr_amount), "tax_amount": "", "realized_fx": _d(w.residual_fx), "prior_period": w.prior_period})  # fmt: skip
    wrev = select(WriteOffReversal, WriteOff).join(WriteOff, WriteOff.id == WriteOffReversal.write_off_id).where(WriteOffReversal.legal_entity_id == legal_entity_id, WriteOffReversal.reversal_date >= start, WriteOffReversal.reversal_date <= end)
    for r, w in (await db.execute(wrev)).all():
        rows.append({"date": r.reversal_date, "row_kind": "write_off_reversal", "ref_kind": "write_off_reversal", "ref_id": r.id, "reference": str(w.id), "document_number": await doc_number(w), "client_id": w.client_id,
                     "currency": w.currency, "amount": _d(-w.amount), "inr_amount": _d(-w.inr_amount), "tax_amount": "", "realized_fx": "", "prior_period": r.prior_period})  # fmt: skip
    rows.sort(key=lambda r: (r["date"], r["row_kind"], str(r["ref_id"])))
    return rows


async def create_export(ctx: RequestContext, db: AsyncSession, data: ExportIn) -> ExportOut:
    if data.to_date < data.from_date:
        raise invalid("invalid_range", "The end date is before the start date")
    template = None
    if data.template_id is not None:
        template = (await db.execute(select(AccountingExportTemplate).where(AccountingExportTemplate.id == data.template_id))).scalar_one_or_none()
        if template is None:
            raise NotFound("Export template not found")
    version = int((await db.execute(select(func.count(AccountingExport.id)).where(AccountingExport.legal_entity_id == data.legal_entity_id))).scalar() or 0) + 1
    export = AccountingExport(
        id=uuid.uuid4(), tenant_id=tenant_of(db), legal_entity_id=data.legal_entity_id, from_date=data.from_date, to_date=data.to_date,
        template_id=data.template_id, format=data.format, version=version, status="queued", created_by=user_id(ctx),
    )  # fmt: skip
    db.add(export)
    await db.flush()
    rows = await collect(db, data.legal_entity_id, data.from_date, data.to_date)
    account_map = dict(template.account_map) if template is not None else {}
    flagged = 0
    for r in rows:
        prev = (
            await db.execute(
                select(AccountingExportItem.export_id).where(AccountingExportItem.doc_kind == r["ref_kind"], AccountingExportItem.doc_id == r["ref_id"], AccountingExportItem.export_id != export.id).order_by(AccountingExportItem.created_at.desc())
            )
        ).scalars().first()
        r["previously_exported_in"] = str(prev) if prev else ""
        flagged += prev is not None
        r["account"] = account_map.get(r["row_kind"], "")
        db.add(AccountingExportItem(id=uuid.uuid4(), tenant_id=export.tenant_id, export_id=export.id, doc_kind=r["ref_kind"], doc_id=r["ref_id"], previously_exported_in=prev))
    export.rows = [{k: (str(v) if not isinstance(v, (bool, str)) else v) for k, v in r.items()} for r in rows]
    export.row_count = len(rows)
    export.flagged_count = flagged
    export.status = "completed"
    await db.flush()
    await db.refresh(export)
    audit.record("accounting.exported", "accounting_export", export.id, session=db, rows=len(rows), flagged=flagged)
    return ExportOut.model_validate(export)


async def list_exports(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID | None = None) -> list[ExportOut]:  # ruff: ignore[unused-function-argument]
    stmt = select(AccountingExport)
    if legal_entity_id:
        stmt = stmt.where(AccountingExport.legal_entity_id == legal_entity_id)
    return [ExportOut.model_validate(e) for e in (await db.execute(stmt.order_by(AccountingExport.created_at.desc()).limit(200))).scalars()]


async def download(ctx: RequestContext, db: AsyncSession, export_id: uuid.UUID) -> tuple[bytes, str, str]:  # ruff: ignore[unused-function-argument]
    export = (await db.execute(select(AccountingExport).where(AccountingExport.id == export_id))).scalar_one_or_none()
    if export is None:
        raise NotFound("Export not found")
    columns = DEFAULT_COLUMNS
    if export.template_id is not None:
        tpl = (await db.execute(select(AccountingExportTemplate).where(AccountingExportTemplate.id == export.template_id))).scalar_one_or_none()
        if tpl is not None and tpl.columns:
            columns = list(tpl.columns)
    name = f"ledger-{export.from_date.isoformat()}-{export.to_date.isoformat()}-v{export.version}"
    if export.format == "xlsx":
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(columns)
        for r in export.rows:
            ws.append([str(r.get(c, "")) for c in columns])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), f"{name}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for r in export.rows:
        writer.writerow([_safe_cell(r.get(c, "")) for c in columns])
    return out.getvalue().encode("utf-8"), f"{name}.csv", "text/csv"


def _safe_cell(v: Any) -> str:
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "@") or (s[:1] == "-" and not _is_number(s)) else s


def _is_number(s: str) -> bool:
    try:
        Decimal(s)
    except Exception:
        return False
    return True
