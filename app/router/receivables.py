# ruff: noqa: E501
"""Receivables, collections, statements, accounting export and reconciliation (Epic 30, FR-153..FR-160):
``/api/v1/receivables`` and ``/api/v1/reconciliation``.

Permissions: ``receivables:read`` (reads), ``payment:record`` (record / allocate / bank import, Idempotency-Key),
``payment:reverse`` (★ reversals), ``tds:write``, ``invoice:write_off``, ``dispute:write``, ``collections:write``,
``accounting:export``, ``reconciliation:read``; settings need ``billing:configure``.
"""

import io
import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import Response, StreamingResponse

from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.receivables import (
    ActivityIn,
    ActivityOut,
    AgingOut,
    AllocateIn,
    AllocationOut,
    BankApplyIn,
    BankImportOut,
    CertificateIn,
    CollectionSettingsIn,
    CollectionSettingsOut,
    CreditBalanceOut,
    DisputeIn,
    DisputeOut,
    DisputeResolveIn,
    DsoOut,
    ExportIn,
    ExportOut,
    ExportTemplateIn,
    ExportTemplateOut,
    OpenDocumentOut,
    PaymentDetailOut,
    PaymentIn,
    PaymentOut,
    PaymentReverseIn,
    QueueRow,
    ReceivablesSettingsOut,
    ReceivablesSettingsUpdate,
    ReconFindingOut,
    ReconRunOut,
    ReminderIn,
    ReminderOut,
    ReverseIn,
    StatementOut,
    TdsOut,
    TdsReceivableRow,
    TdsSectionIn,
    TdsSectionOut,
    WriteOffIn,
    WriteOffRequestOut,
)
from app.services.receivables import (
    accounting_export,
    aging,
    bank_import,
    collections,
    credits,
    disputes,
    payments,
    reconciliation,
    statements,
    tds_records,
    write_offs,
)
from app.services.receivables import settings as settings_svc

router = APIRouter(prefix="/receivables", tags=["receivables"])
recon_router = APIRouter(prefix="/reconciliation", tags=["receivables"])
DB = TenantDBSessionDep
Idem = Annotated[IdempotencyGuard, Depends(idempotency())]

Read = Annotated[Principal, Depends(require("receivables:read"))]
Record = Annotated[Principal, Depends(require("payment:record"))]
Reverse = Annotated[Principal, Depends(require("payment:reverse"))]
TdsWrite = Annotated[Principal, Depends(require("tds:write"))]
WriteOffPerm = Annotated[Principal, Depends(require("invoice:write_off"))]
DisputeWrite = Annotated[Principal, Depends(require("dispute:write"))]
Collect = Annotated[Principal, Depends(require("collections:write"))]
Export = Annotated[Principal, Depends(require("accounting:export"))]
Recon = Annotated[Principal, Depends(require("reconciliation:read"))]
Configure = Annotated[Principal, Depends(require("billing:configure"))]


def _file(data: bytes, filename: str, media_type: str) -> Response:
    return Response(content=data, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "private, no-store"})


# ---------------------------------------------------------------- settings
@router.get("/settings", response_model=ReceivablesSettingsOut)
async def get_settings(_: Read, db: DB) -> Any:
    return await settings_svc.get_settings(get_context(), db)


@router.patch("/settings", response_model=ReceivablesSettingsOut)
async def update_settings(data: ReceivablesSettingsUpdate, _: Configure, db: DB) -> Any:
    return await settings_svc.update_settings(get_context(), db, data)


# ---------------------------------------------------------------- payments
@router.post("/payments", response_model=PaymentDetailOut, status_code=status.HTTP_201_CREATED)
async def record_payment(data: PaymentIn, _: Record, db: DB, idem: Idem) -> Any:
    return await idem.run(lambda: payments.record(get_context(), db, data))


@router.get("/payments", response_model=list[PaymentOut])
async def list_payments(
    _: Read, db: DB, client_id: uuid.UUID | None = None, legal_entity_id: uuid.UUID | None = None,
    status_: Annotated[str | None, Query(alias="status")] = None, date_from: date | None = None, date_to: date | None = None,
) -> Any:  # fmt: skip
    return await payments.list_payments(get_context(), db, client_id=client_id, legal_entity_id=legal_entity_id, status=status_, date_from=date_from, date_to=date_to)


@router.get("/payments/{payment_id}", response_model=PaymentDetailOut)
async def get_payment(payment_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await payments.get_payment(get_context(), db, payment_id)


@router.post("/payments/{payment_id}/allocate", response_model=PaymentDetailOut)
async def allocate(payment_id: uuid.UUID, data: AllocateIn, _: Record, db: DB, idem: Idem) -> Any:
    return await idem.run(lambda: payments.allocate(get_context(), db, payment_id, data))


@router.post("/payments/{payment_id}/reverse", response_model=PaymentDetailOut)
async def reverse_payment(payment_id: uuid.UUID, data: PaymentReverseIn, _: Reverse, db: DB, idem: Idem) -> Any:
    return await idem.run(lambda: payments.reverse(get_context(), db, payment_id, data))


@router.post("/allocations/{allocation_id}/reverse", response_model=AllocationOut)
async def reverse_allocation(allocation_id: uuid.UUID, data: ReverseIn, _: Reverse, db: DB, idem: Idem) -> Any:
    return await idem.run(lambda: payments.reverse_allocation(get_context(), db, allocation_id, data))


@router.get("/documents/{document_id}/allocations", response_model=list[AllocationOut])
async def document_allocations(document_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await payments.allocations_for(db, document_id=document_id)


@router.get("/open-documents", response_model=list[OpenDocumentOut])
async def open_documents(_: Read, db: DB, client_id: uuid.UUID, legal_entity_id: uuid.UUID | None = None, currency: str | None = None) -> Any:
    return await payments.open_documents(get_context(), db, client_id, legal_entity_id, currency.upper() if currency else None)


@router.get("/clients/{client_id}/credits", response_model=list[CreditBalanceOut])
async def client_credits(client_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await credits.balances_for_client(get_context(), db, client_id)


@router.post("/credit-notes/{credit_note_id}/apply", response_model=list[uuid.UUID])
async def apply_credit_note_credit(credit_note_id: uuid.UUID, data: AllocateIn, _: Record, db: DB, idem: Idem) -> Any:
    return await idem.run(lambda: credits.apply_cn_credit(get_context(), db, credit_note_id, data))


# ---------------------------------------------------------------- bank import
@router.post("/bank-imports", response_model=BankImportOut, status_code=status.HTTP_201_CREATED)
async def bank_import_preview(
    _: Record, db: DB, legal_entity_id: Annotated[uuid.UUID, Form()], file: Annotated[UploadFile, File()],
    bank_account_id: Annotated[uuid.UUID | None, Form()] = None,
) -> Any:  # fmt: skip
    """Multipart upload: the file's SHA-256 (``file_import_batches``) is the dedupe key -- the same file returns
    the same batch -- so no Idempotency-Key is read (the guard would consume the multipart stream)."""
    data = await file.read()
    return await bank_import.preview(get_context(), db, legal_entity_id, bank_account_id, data)


@router.post("/bank-imports/{batch_id}/apply", response_model=BankImportOut)
async def bank_import_apply(batch_id: uuid.UUID, data: BankApplyIn, _: Record, db: DB, idem: Idem) -> Any:
    return await idem.run(lambda: bank_import.apply(get_context(), db, batch_id, data))


# ---------------------------------------------------------------- TDS
@router.get("/tds-sections", response_model=list[TdsSectionOut])
async def list_tds_sections(_: Read, db: DB) -> Any:
    return await tds_records.list_sections(get_context(), db)


@router.post("/tds-sections", response_model=TdsSectionOut, status_code=status.HTTP_201_CREATED)
async def create_tds_section(data: TdsSectionIn, _: TdsWrite, db: DB) -> Any:
    return await tds_records.create_section(get_context(), db, data)


@router.get("/tds-deductions", response_model=list[TdsOut])
async def list_tds(_: Read, db: DB, quarter: str | None = None, client_id: uuid.UUID | None = None, status_: Annotated[str | None, Query(alias="status")] = None, kind: str | None = None) -> Any:
    return await tds_records.list_deductions(get_context(), db, quarter=quarter, client_id=client_id, status=status_, kind=kind)


@router.patch("/tds-deductions/{tds_id}/certificate", response_model=TdsOut)
async def tds_certificate(tds_id: uuid.UUID, data: CertificateIn, _: TdsWrite, db: DB) -> Any:
    return await tds_records.update_certificate(get_context(), db, tds_id, data)


@router.post("/tds-deductions/{tds_id}/reverse", response_model=TdsOut)
async def tds_reverse(tds_id: uuid.UUID, data: ReverseIn, _: TdsWrite, db: DB, idem: Idem) -> Any:
    return await idem.run(lambda: tds_records.reverse(get_context(), db, tds_id, data))


@router.get("/tds-receivable", response_model=list[TdsReceivableRow])
async def tds_receivable(_: Read, db: DB, quarter: str | None = None, client_id: uuid.UUID | None = None, status_: Annotated[str | None, Query(alias="status")] = None) -> Any:
    return await tds_records.receivable(get_context(), db, quarter=quarter, client_id=client_id, status=status_)


# ---------------------------------------------------------------- write-offs, disputes
@router.post("/write-offs", response_model=WriteOffRequestOut, status_code=status.HTTP_201_CREATED)
async def request_write_off(data: WriteOffIn, _: WriteOffPerm, db: DB, idem: Idem) -> Any:
    return await idem.run(lambda: write_offs.request(get_context(), db, data))


@router.get("/write-offs", response_model=list[WriteOffRequestOut])
async def list_write_offs(_: Read, db: DB, status_: Annotated[str | None, Query(alias="status")] = None) -> Any:
    return await write_offs.list_requests(get_context(), db, status_)


@router.get("/write-offs/{request_id}", response_model=WriteOffRequestOut)
async def get_write_off(request_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await write_offs.get_request(get_context(), db, request_id)


@router.post("/write-offs/executed/{write_off_id}/reverse", status_code=status.HTTP_204_NO_CONTENT)
async def reverse_write_off(write_off_id: uuid.UUID, data: ReverseIn, _: WriteOffPerm, db: DB) -> None:
    await write_offs.reverse(get_context(), db, write_off_id, data)


@router.post("/disputes", response_model=DisputeOut, status_code=status.HTTP_201_CREATED)
async def open_dispute(data: DisputeIn, _: DisputeWrite, db: DB) -> Any:
    return await disputes.open_dispute(get_context(), db, data)


@router.get("/disputes", response_model=list[DisputeOut])
async def list_disputes(_: Read, db: DB, client_id: uuid.UUID | None = None, status_: Annotated[str | None, Query(alias="status")] = None) -> Any:
    return await disputes.list_disputes(get_context(), db, client_id=client_id, status=status_)


@router.post("/disputes/{dispute_id}/resolve", response_model=DisputeOut)
async def resolve_dispute(dispute_id: uuid.UUID, data: DisputeResolveIn, _: DisputeWrite, db: DB) -> Any:
    return await disputes.resolve(get_context(), db, dispute_id, data)


# ---------------------------------------------------------------- aging, DSO
@router.get("/aging", response_model=AgingOut)
async def get_aging(
    _: Read, db: DB, legal_entity_id: uuid.UUID, as_of: date | None = None, group_by: Literal["entity", "client", "document"] = "client",
    client_id: uuid.UUID | None = None, format: Literal["json", "csv", "xlsx"] = "json",
) -> Any:  # fmt: skip
    from app.services.receivables.common import today

    out = await aging.query(get_context(), db, legal_entity_id, as_of or await today(db), group_by, client_id)
    if format == "json":
        return out
    header = ["label", "currency", *out.bucket_labels, "total_open", "total_open_inr", "disputed", "unallocated_credit", "link"]
    lines = [header]
    for r in out.rows:
        link = f"/billing/{'invoices' if r.document_kind == 'invoice' else 'debit-notes'}/{r.document_id}" if r.document_id else (f"/clients/{r.client_id}" if r.client_id else "")
        lines.append([r.label, r.currency, *[str(r.buckets[b]) for b in out.bucket_labels], str(r.total_open), str(r.total_open_inr), str(r.disputed), str(r.unallocated_credit), link])
    name = f"aging-{out.as_of.isoformat()}"
    if format == "xlsx":
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        for line in lines:
            ws.append(line)
        buf = io.BytesIO()
        wb.save(buf)
        return _file(buf.getvalue(), f"{name}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    import csv

    sio = io.StringIO()
    csv.writer(sio).writerows(lines)
    return _file(sio.getvalue().encode(), f"{name}.csv", "text/csv")


@router.get("/dso", response_model=DsoOut)
async def get_dso(_: Read, db: DB, legal_entity_id: uuid.UUID, period_start: date, period_end: date) -> Any:
    return await aging.dso(get_context(), db, legal_entity_id, period_start, period_end)


# ---------------------------------------------------------------- collections
@router.get("/collections/queue", response_model=list[QueueRow])
async def collections_queue(_: Read, db: DB, legal_entity_id: uuid.UUID | None = None, client_id: uuid.UUID | None = None, min_days: int = 1) -> Any:
    return await collections.queue(get_context(), db, legal_entity_id=legal_entity_id, client_id=client_id, min_days=min_days)


@router.get("/collections/activities", response_model=list[ActivityOut])
async def list_activities(_: Read, db: DB, client_id: uuid.UUID | None = None, ptp_status: str | None = None) -> Any:
    return await collections.list_activities(get_context(), db, client_id, ptp_status)


@router.post("/collections/activities", response_model=ActivityOut, status_code=status.HTTP_201_CREATED)
async def log_activity(data: ActivityIn, _: Collect, db: DB) -> Any:
    return await collections.log_activity(get_context(), db, data)


@router.get("/collections/clients/{client_id}/settings", response_model=CollectionSettingsOut)
async def get_collection_settings(client_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await collections.get_client_settings(get_context(), db, client_id)


@router.put("/collections/clients/{client_id}/settings", response_model=CollectionSettingsOut)
async def put_collection_settings(client_id: uuid.UUID, data: CollectionSettingsIn, _: Collect, db: DB) -> Any:
    return await collections.put_client_settings(get_context(), db, client_id, data)


@router.post("/collections/reminders", response_model=ReminderOut)
async def send_reminders(data: ReminderIn, _: Collect, db: DB) -> Any:
    return await collections.send_reminders(get_context(), db, data)


# ---------------------------------------------------------------- statements
@router.get("/statements", response_model=StatementOut)
async def get_statement(
    _: Read, db: DB, client_id: uuid.UUID, start: date, end: date, currency: str = "INR", legal_entity_id: uuid.UUID | None = None,
    format: Literal["json", "pdf"] = "json",
) -> Any:  # fmt: skip
    out = await statements.build(get_context(), db, client_id, currency.upper(), start, end, legal_entity_id)
    if format == "pdf":
        from app.services.receivables.aging import _client_names

        name = (await _client_names(db, {client_id})).get(client_id, "Client")
        return StreamingResponse(io.BytesIO(statements.to_pdf(out, name)), media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="statement-{start}-{end}.pdf"'})
    return out


# ---------------------------------------------------------------- accounting export
@router.get("/accounting-export-templates", response_model=list[ExportTemplateOut])
async def list_export_templates(_: Export, db: DB) -> Any:
    return await accounting_export.list_templates(get_context(), db)


@router.post("/accounting-export-templates", response_model=ExportTemplateOut, status_code=status.HTTP_201_CREATED)
async def create_export_template(data: ExportTemplateIn, _: Export, db: DB) -> Any:
    return await accounting_export.create_template(get_context(), db, data)


@router.post("/accounting-exports", response_model=ExportOut, status_code=status.HTTP_201_CREATED)
async def create_export(data: ExportIn, _: Export, db: DB, idem: Idem) -> Any:
    return await idem.run(lambda: accounting_export.create_export(get_context(), db, data))


@router.get("/accounting-exports", response_model=list[ExportOut])
async def list_exports(_: Export, db: DB, legal_entity_id: uuid.UUID | None = None) -> Any:
    return await accounting_export.list_exports(get_context(), db, legal_entity_id)


@router.get("/accounting-exports/{export_id}/download")
async def download_export(export_id: uuid.UUID, _: Export, db: DB) -> Response:
    data, name, media = await accounting_export.download(get_context(), db, export_id)
    return _file(data, name, media)


# ---------------------------------------------------------------- reconciliation
@recon_router.get("/runs", response_model=list[ReconRunOut])
async def list_runs(_: Recon, db: DB) -> Any:
    return await reconciliation.list_runs(get_context(), db)


@recon_router.post("/runs", response_model=ReconRunOut, status_code=status.HTTP_201_CREATED)
async def start_run(_: Recon, db: DB, as_of: date | None = None) -> Any:
    return await reconciliation.run(get_context(), db, as_of)


@recon_router.get("/runs/{run_id}/findings", response_model=list[ReconFindingOut])
async def run_findings(run_id: uuid.UUID, _: Recon, db: DB) -> Any:
    return await reconciliation.findings(get_context(), db, run_id)
