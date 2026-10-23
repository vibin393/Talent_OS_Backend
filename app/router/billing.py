# ruff: file-ignore[line-too-long]
"""Billing & GST invoicing (Epic 29, FR-143..FR-152): ``/api/v1/billing``.

Permissions: ``billing:read`` (lists, details, PDFs), ``billing:write`` (rules, manual items, runs, drafts),
``billing:configure`` (settings, SAC, tax rates, Client billing settings, integrity), ``invoice:issue`` (sensitive:
issue invoices and notes), ``credit_note:write`` (credit / debit notes), ``einvoice:write`` (IRN capture, export,
import, IRN cancellation). Every number-issuing / money-moving endpoint takes an ``Idempotency-Key``.
"""

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import Response

from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.billing import (
    BillingRuleIn,
    BillingRuleOut,
    BillingSettingsOut,
    BillingSettingsUpdate,
    ClientBillingSettingsIn,
    ClientBillingSettingsOut,
    CreditNoteIn,
    DebitNoteIn,
    DocDetailOut,
    DocSummaryOut,
    EinvoiceApplyOut,
    EinvoiceImportOut,
    FxOverrideIn,
    IntegrityFindingOut,
    IrnCancelIn,
    IrnIn,
    IssueBatchIn,
    IssueBatchOut,
    IssueIn,
    LinePatch,
    ManualInvoiceIn,
    ManualItemIn,
    ManualItemOut,
    ManualLineIn,
    NoteCapsOut,
    RunCreate,
    RunDetailOut,
    RunOut,
    SacCodeIn,
    SacCodeOut,
    SendIn,
    TaxRateIn,
    TaxRateOut,
)
from app.services import files_service
from app.services.billing import einvoice, integrity, invoices, issue, notes, pdf, portal, rates, rules, runs

router = APIRouter(prefix="/billing", tags=["billing"])
DB = TenantDBSessionDep

Read = Annotated[Principal, Depends(require("billing:read"))]
Write = Annotated[Principal, Depends(require("billing:write"))]
Configure = Annotated[Principal, Depends(require("billing:configure"))]
Issue = Annotated[Principal, Depends(require("invoice:issue"))]
NoteWrite = Annotated[Principal, Depends(require("credit_note:write"))]
EinvoiceWrite = Annotated[Principal, Depends(require("einvoice:write"))]
NoteKind = Literal["credit-notes", "debit-notes"]
_KIND = {"credit-notes": "credit_note", "debit-notes": "debit_note", "invoices": "invoice"}


def _pdf(data: bytes, filename: str) -> Response:
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------------------------------------------------------------- settings / tax master
@router.get("/settings", response_model=BillingSettingsOut)
async def get_settings(_: Read, db: DB) -> Any:
    return await rules.get_settings(get_context(), db)


@router.patch("/settings", response_model=BillingSettingsOut)
async def update_settings(data: BillingSettingsUpdate, _: Configure, db: DB) -> Any:
    return await rules.update_settings(get_context(), db, data)


@router.get("/sac-codes", response_model=list[SacCodeOut])
async def list_sac(_: Read, db: DB) -> Any:
    return await rates.list_sac(get_context(), db)


@router.put("/sac-codes", response_model=SacCodeOut)
async def upsert_sac(data: SacCodeIn, _: Configure, db: DB) -> Any:
    return await rates.upsert_sac(get_context(), db, data)


@router.get("/tax-rates", response_model=list[TaxRateOut])
async def list_tax_rates(_: Read, db: DB) -> Any:
    return await rates.list_rates(get_context(), db)


@router.post("/tax-rates", response_model=TaxRateOut, status_code=status.HTTP_201_CREATED)
async def add_tax_rate(data: TaxRateIn, _: Configure, db: DB) -> Any:
    return await rates.add_rate(get_context(), db, data)


@router.get("/clients/{client_id}/settings", response_model=ClientBillingSettingsOut)
async def get_client_settings(client_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await rules.get_client_settings(get_context(), db, client_id)


@router.put("/clients/{client_id}/settings", response_model=ClientBillingSettingsOut)
async def put_client_settings(
    client_id: uuid.UUID, data: ClientBillingSettingsIn, _: Configure, db: DB
) -> Any:
    return await rules.put_client_settings(get_context(), db, client_id, data)


# ---------------------------------------------------------------- rules / manual items
@router.get("/placements/{placement_id}/rule", response_model=BillingRuleOut | None)
async def get_rule(placement_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await rules.get_rule(get_context(), db, placement_id)


@router.put("/placements/{placement_id}/rule", response_model=BillingRuleOut)
async def put_rule(placement_id: uuid.UUID, data: BillingRuleIn, _: Write, db: DB) -> Any:
    return await rules.put_rule(get_context(), db, placement_id, data)


@router.post("/manual-items", response_model=ManualItemOut, status_code=status.HTTP_201_CREATED)
async def create_manual_item(data: ManualItemIn, _: Write, db: DB) -> Any:
    return await rules.create_manual_item(get_context(), db, data)


# ---------------------------------------------------------------- invoice runs
@router.post("/runs", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    data: RunCreate, _: Write, db: DB, idem: Annotated[IdempotencyGuard, Depends(idempotency())]
) -> Any:
    return await idem.run(lambda: runs.create_run(get_context(), db, data))


@router.get("/runs", response_model=list[RunOut])
async def list_runs(_: Read, db: DB) -> Any:
    return await runs.list_runs(get_context(), db)


@router.get("/runs/{run_id}", response_model=RunDetailOut)
async def get_run(run_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await runs.get_run(get_context(), db, run_id)


@router.post("/runs/{run_id}/discard", response_model=RunDetailOut)
async def discard_run(run_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await runs.discard_run(get_context(), db, run_id)


# ---------------------------------------------------------------- invoices
@router.get("/invoices", response_model=list[DocSummaryOut])
async def list_invoices(
    _: Read,
    db: DB,
    status_: Annotated[str | None, Query(alias="status")] = None,
    einvoice_status: str | None = None,
    delivery_status: str | None = None,
    client_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Any:
    return await invoices.list_documents(
        get_context(), db, "invoice", status=status_, einvoice_status=einvoice_status, delivery_status=delivery_status,
        client_id=client_id, run_id=run_id, date_from=date_from, date_to=date_to,
    )  # fmt: skip


@router.post("/invoices/manual", response_model=DocDetailOut, status_code=status.HTTP_201_CREATED)
async def create_manual_invoice(data: ManualInvoiceIn, _: Write, db: DB) -> Any:
    return await invoices.create_manual(get_context(), db, data)


@router.post("/invoices/issue-batch", response_model=IssueBatchOut)
async def issue_batch(
    data: IssueBatchIn, _: Issue, db: DB, idem: Annotated[IdempotencyGuard, Depends(idempotency())]
) -> Any:
    return await idem.run(lambda: issue.issue_batch(get_context(), db, list(data.invoice_ids), data.doc_date))


@router.get("/invoices/{invoice_id}", response_model=DocDetailOut)
async def get_invoice(invoice_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await invoices.detail(get_context(), db, "invoice", invoice_id)


@router.get("/invoices/{invoice_id}/sources")
async def invoice_sources(invoice_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await invoices.sources(get_context(), db, "invoice", invoice_id)


@router.post("/invoices/{invoice_id}/lines", response_model=DocDetailOut, status_code=status.HTTP_201_CREATED)
async def add_line(invoice_id: uuid.UUID, data: ManualLineIn, _: Write, db: DB) -> Any:
    return await invoices.add_manual_line(get_context(), db, invoice_id, data)


@router.patch("/invoices/{invoice_id}/lines/{line_id}", response_model=DocDetailOut)
async def patch_line(invoice_id: uuid.UUID, line_id: uuid.UUID, data: LinePatch, _: Write, db: DB) -> Any:
    return await invoices.patch_line(get_context(), db, invoice_id, line_id, data)


@router.delete("/invoices/{invoice_id}/lines/{line_id}", response_model=DocDetailOut)
async def remove_line(invoice_id: uuid.UUID, line_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await invoices.remove_line(get_context(), db, invoice_id, line_id)


@router.post("/invoices/{invoice_id}/recompute", response_model=DocDetailOut)
async def recompute(invoice_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await invoices.recompute_draft(get_context(), db, invoice_id)


@router.put("/invoices/{invoice_id}/fx", response_model=DocDetailOut)
async def set_fx(invoice_id: uuid.UUID, data: FxOverrideIn, _: Write, db: DB) -> Any:
    return await invoices.set_fx(get_context(), db, invoice_id, data)


@router.post("/invoices/{invoice_id}/discard", response_model=DocDetailOut)
async def discard_invoice(invoice_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await invoices.discard(get_context(), db, invoice_id)


@router.post("/invoices/{invoice_id}/submit", response_model=DocDetailOut)
async def submit_invoice(invoice_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await issue.submit(get_context(), db, invoice_id)


@router.post("/invoices/{invoice_id}/issue", response_model=DocDetailOut)
async def issue_invoice(
    invoice_id: uuid.UUID,
    _: Issue,
    db: DB,
    idem: Annotated[IdempotencyGuard, Depends(idempotency())],
    data: IssueIn | None = None,
) -> Any:
    return await idem.run(
        lambda: issue.issue_invoice(get_context(), db, invoice_id, data.doc_date if data else None)
    )


@router.get("/invoices/{invoice_id}/pdf")
async def invoice_pdf(invoice_id: uuid.UUID, _: Read, db: DB) -> Response:
    return _pdf(*await pdf.render(get_context(), db, "invoice", invoice_id))


@router.post("/invoices/{invoice_id}/send", response_model=DocDetailOut)
async def send_invoice(invoice_id: uuid.UUID, data: SendIn, _: Write, db: DB) -> Any:
    return await portal.send_document(get_context(), db, "invoice", invoice_id, data)


@router.get("/invoices/{invoice_id}/note-caps", response_model=NoteCapsOut)
async def note_caps(invoice_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await notes.note_caps(get_context(), db, invoice_id)


@router.post(
    "/invoices/{invoice_id}/credit-notes", response_model=DocDetailOut, status_code=status.HTTP_201_CREATED
)
async def create_credit_note(invoice_id: uuid.UUID, data: CreditNoteIn, _: NoteWrite, db: DB) -> Any:
    return await notes.create_credit_note(get_context(), db, invoice_id, data)


@router.post(
    "/invoices/{invoice_id}/debit-notes", response_model=DocDetailOut, status_code=status.HTTP_201_CREATED
)
async def create_debit_note(invoice_id: uuid.UUID, data: DebitNoteIn, _: NoteWrite, db: DB) -> Any:
    return await notes.create_debit_note(get_context(), db, invoice_id, data)


@router.post("/invoices/{invoice_id}/irn-cancel", response_model=DocDetailOut)
async def cancel_irn(
    invoice_id: uuid.UUID,
    data: IrnCancelIn,
    _: EinvoiceWrite,
    db: DB,
    idem: Annotated[IdempotencyGuard, Depends(idempotency())],
) -> Any:
    return await idem.run(lambda: einvoice.cancel_irn(get_context(), db, invoice_id, data))


# ---------------------------------------------------------------- e-invoice
@router.get("/einvoice/pending", response_model=list[DocSummaryOut])
async def einvoice_pending(_: Read, db: DB, gst_registration_id: uuid.UUID | None = None) -> Any:
    return await einvoice.pending(get_context(), db, gst_registration_id)


@router.get("/einvoice/export")
async def einvoice_export(
    _: EinvoiceWrite,
    db: DB,
    gst_registration_id: uuid.UUID | None = None,
    doc_ids: Annotated[list[uuid.UUID] | None, Query()] = None,
) -> Response:
    body = await einvoice.export(get_context(), db, gst_registration_id, doc_ids)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="einvoice-export.json"'},
    )


@router.post("/einvoice/import", response_model=EinvoiceImportOut, status_code=status.HTTP_201_CREATED)
async def einvoice_import(_: EinvoiceWrite, db: DB, file: Annotated[UploadFile, File()]) -> Any:
    content = await files_service.read_upload(file, 5)
    return await einvoice.import_preview(get_context(), db, content, file.filename or "response.json")


@router.post("/einvoice/import/{batch_id}/apply", response_model=EinvoiceApplyOut)
async def einvoice_apply(
    batch_id: uuid.UUID, _: EinvoiceWrite, db: DB, idem: Annotated[IdempotencyGuard, Depends(idempotency())]
) -> Any:
    return await idem.run(lambda: einvoice.import_apply(get_context(), db, batch_id))


# ---------------------------------------------------------------- integrity
@router.post("/integrity/run", response_model=list[IntegrityFindingOut])
async def run_integrity(_: Configure, db: DB) -> Any:
    findings = await integrity.run_all(get_context(), db, date.today())
    return [
        IntegrityFindingOut(
            check=f.check,
            severity=f.severity,
            subject_kind=f.subject_kind,
            subject_id=f.subject_id,
            details=dict(f.details),
        )
        for f in findings
    ]


# ---------------------------------------------------------------- credit / debit notes
@router.get("/{kind}", response_model=list[DocSummaryOut])
async def list_notes(
    kind: NoteKind,
    _: Read,
    db: DB,
    invoice_id: uuid.UUID | None = None,
    status_: Annotated[str | None, Query(alias="status")] = None,
) -> Any:
    return await invoices.list_documents(
        get_context(), db, _KIND[kind], status=status_, original_invoice_id=invoice_id
    )


@router.get("/{kind}/{note_id}", response_model=DocDetailOut)
async def get_note(kind: NoteKind, note_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await invoices.detail(get_context(), db, _KIND[kind], note_id)


@router.post("/{kind}/{note_id}/issue", response_model=DocDetailOut)
async def issue_note(
    kind: NoteKind,
    note_id: uuid.UUID,
    _: Issue,
    db: DB,
    idem: Annotated[IdempotencyGuard, Depends(idempotency())],
    data: IssueIn | None = None,
) -> Any:
    return await idem.run(
        lambda: notes.issue_note(get_context(), db, _KIND[kind], note_id, data.doc_date if data else None)
    )


@router.post("/{kind}/{note_id}/discard", response_model=DocDetailOut)
async def discard_note(kind: NoteKind, note_id: uuid.UUID, _: NoteWrite, db: DB) -> Any:
    return await notes.discard_note(get_context(), db, _KIND[kind], note_id)


@router.get("/{kind}/{note_id}/pdf")
async def note_pdf(kind: NoteKind, note_id: uuid.UUID, _: Read, db: DB) -> Response:
    return _pdf(*await pdf.render(get_context(), db, _KIND[kind], note_id))


@router.post("/{kind}/{note_id}/send", response_model=DocDetailOut)
async def send_note(kind: NoteKind, note_id: uuid.UUID, data: SendIn, _: Write, db: DB) -> Any:
    return await portal.send_document(get_context(), db, _KIND[kind], note_id, data)


@router.post("/{kind}/{doc_id}/irn", response_model=DocDetailOut)
async def record_irn(
    kind: Literal["invoices", "credit-notes", "debit-notes"],
    doc_id: uuid.UUID,
    data: IrnIn,
    _: EinvoiceWrite,
    db: DB,
    idem: Annotated[IdempotencyGuard, Depends(idempotency())],
) -> Any:
    return await idem.run(lambda: einvoice.record_irn(get_context(), db, _KIND[kind], doc_id, data))
