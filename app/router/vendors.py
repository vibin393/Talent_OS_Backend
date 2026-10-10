"""Vendors (Epic 17, FR-93..FR-99): vendor records, status, GSTINs, bank accounts, documents, settings, demand
releases, the vendor submission review queue, performance and vendor payable statements.

Permissions: ``vendor:read|write``, ``vendor:release``, ``vendor_submission:review``, ``vendor:performance``,
``identity:view`` (reveal), ``payroll:read|generate|finalize`` (statements). External principals never reach
these routes (AD-26). Each endpoint calls one service function.
"""

import uuid
from datetime import date
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.vendors import (
    AcceptIn,
    BankAccountIn,
    BankAccountOut,
    GstinIn,
    GstinOut,
    MarkDuplicateIn,
    RejectIn,
    ReleaseBatchOut,
    ReleaseCreate,
    ReleaseOut,
    ReopenIn,
    RevealIn,
    RevealOut,
    StatementBuildIn,
    StatementBuildOut,
    StatementDetailOut,
    StatementOut,
    VendorCreate,
    VendorDetailOut,
    VendorDocumentOut,
    VendorOut,
    VendorScorecard,
    VendorSettingsOut,
    VendorSettingsUpdate,
    VendorStatus,
    VendorSubmissionOut,
    VendorTier,
    VendorTransitionIn,
    VendorUpdate,
)
from app.services import files_service
from app.services.payroll import vendor_statements
from app.services.vendors import performance, records, releases, review, submissions

router = APIRouter(tags=["vendors"])
DB = TenantDBSessionDep

VendorRead = Annotated[Principal, Depends(require("vendor:read"))]
VendorWrite = Annotated[Principal, Depends(require("vendor:write"))]
VendorRelease = Annotated[Principal, Depends(require("vendor:release"))]
Review = Annotated[Principal, Depends(require("vendor_submission:review"))]
Performance = Annotated[Principal, Depends(require("vendor:performance"))]
IdentityView = Annotated[Principal, Depends(require("identity:view"))]
PayrollRead = Annotated[Principal, Depends(require("payroll:read"))]
PayrollGenerate = Annotated[Principal, Depends(require("payroll:generate"))]
PayrollFinalize = Annotated[Principal, Depends(require("payroll:finalize"))]


def _file(result: files_service.Download) -> Response:
    return Response(
        content=result.data,
        media_type=result.content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(result.filename)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


# ---------------------------------------------------------------- settings + league (before /vendors/{id})
@router.get("/vendors/settings", response_model=VendorSettingsOut)
async def get_settings(_: VendorRead, db: DB) -> Any:
    return await records.get_settings(get_context(), db)


@router.patch("/vendors/settings", response_model=VendorSettingsOut)
async def update_settings(body: VendorSettingsUpdate, _: VendorWrite, db: DB) -> Any:
    return await records.update_settings(get_context(), db, body)


@router.get("/vendors/performance", response_model=list[VendorScorecard])
async def league(_: Performance, db: DB, period_from: Annotated[date | None, Query(alias="from")] = None,
                 period_to: Annotated[date | None, Query(alias="to")] = None) -> Any:  # fmt: skip
    return await performance.league(get_context(), db, period_from, period_to)


# ---------------------------------------------------------------- vendors
@router.get("/vendors", response_model=list[VendorOut])
async def list_vendors(
    _: VendorRead,
    db: DB,
    status_: Annotated[VendorStatus | None, Query(alias="status")] = None,
    tier: VendorTier | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> Any:
    return await records.list_vendors(get_context(), db, status=status_, tier=tier, q=q)


@router.post("/vendors", response_model=VendorDetailOut, status_code=status.HTTP_201_CREATED)
async def create_vendor(body: VendorCreate, _: VendorWrite, db: DB) -> Any:
    return await records.create_vendor(get_context(), db, body)


@router.get("/vendors/{vendor_id}", response_model=VendorDetailOut)
async def get_vendor(vendor_id: uuid.UUID, _: VendorRead, db: DB) -> Any:
    return await records.get_vendor_detail(get_context(), db, vendor_id)


@router.patch("/vendors/{vendor_id}", response_model=VendorDetailOut)
async def update_vendor(vendor_id: uuid.UUID, body: VendorUpdate, _: VendorWrite, db: DB) -> Any:
    return await records.update_vendor(get_context(), db, vendor_id, body)


@router.post("/vendors/{vendor_id}/transition", response_model=VendorDetailOut)
async def transition_vendor(vendor_id: uuid.UUID, body: VendorTransitionIn, _: VendorWrite, db: DB) -> Any:
    return await records.transition_vendor(get_context(), db, vendor_id, body.to, reason=body.reason)


@router.post("/vendors/{vendor_id}/gstins", response_model=GstinOut, status_code=status.HTTP_201_CREATED)
async def add_gstin(vendor_id: uuid.UUID, body: GstinIn, _: VendorWrite, db: DB) -> Any:
    return await records.add_gstin(get_context(), db, vendor_id, body)


@router.delete("/vendors/{vendor_id}/gstins/{gstin_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_gstin(vendor_id: uuid.UUID, gstin_id: uuid.UUID, _: VendorWrite, db: DB) -> Response:
    await records.delete_gstin(get_context(), db, vendor_id, gstin_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/vendors/{vendor_id}/bank-accounts", response_model=BankAccountOut, status_code=status.HTTP_201_CREATED
)
async def request_bank_account(vendor_id: uuid.UUID, body: BankAccountIn, _: VendorWrite, db: DB) -> Any:
    return await records.request_bank_account(get_context(), db, vendor_id, body)


@router.post("/vendors/{vendor_id}/reveal", response_model=RevealOut)
async def reveal(vendor_id: uuid.UUID, body: RevealIn, _: VendorRead, __: IdentityView, db: DB) -> Any:
    value = await records.reveal(get_context(), db, vendor_id, body.field, body.bank_account_id)
    return RevealOut(field=body.field, value=value)


@router.get("/vendors/{vendor_id}/documents", response_model=list[VendorDocumentOut])
async def list_documents(vendor_id: uuid.UUID, _: VendorRead, db: DB) -> Any:
    return await records.list_documents(get_context(), db, vendor_id)


@router.post(
    "/vendors/{vendor_id}/documents", response_model=VendorDocumentOut, status_code=status.HTTP_201_CREATED
)
async def upload_document(
    vendor_id: uuid.UUID,
    _: VendorWrite,
    db: DB,
    file: Annotated[UploadFile, File()],
    category: Annotated[Literal["agreement", "insurance", "registration", "other"], Form()],
    title: Annotated[str | None, Form(max_length=300)] = None,
    expires_on: Annotated[date | None, Form()] = None,
) -> Any:
    data = await files_service.read_upload(file)
    return await records.upload_document(
        get_context(),
        db,
        vendor_id,
        data=data,
        filename=file.filename,
        category=category,
        title=title,
        expires_on=expires_on,
    )


@router.post("/vendors/{vendor_id}/documents/{document_id}/reviewed", status_code=status.HTTP_204_NO_CONTENT)
async def mark_document_reviewed(
    vendor_id: uuid.UUID, document_id: uuid.UUID, _: VendorWrite, db: DB
) -> Response:
    await records.mark_upload_reviewed(get_context(), db, vendor_id, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/vendors/{vendor_id}/releases", response_model=list[ReleaseOut])
async def vendor_releases(vendor_id: uuid.UUID, _: VendorRead, db: DB) -> Any:
    await records.get_vendor_detail(get_context(), db, vendor_id)
    return await releases.list_for_vendor(get_context(), db, vendor_id)


@router.get("/vendors/{vendor_id}/performance", response_model=VendorScorecard)
async def vendor_performance(
    vendor_id: uuid.UUID,
    _: Performance,
    db: DB,
    period_from: Annotated[date | None, Query(alias="from")] = None,
    period_to: Annotated[date | None, Query(alias="to")] = None,
) -> Any:
    return await performance.scorecard(get_context(), db, vendor_id, period_from, period_to)


# ---------------------------------------------------------------- releases
@router.post(
    "/demands/{demand_id}/vendor-releases",
    response_model=ReleaseBatchOut,
    status_code=status.HTTP_201_CREATED,
)
async def release_demand(demand_id: uuid.UUID, body: ReleaseCreate, _: VendorRelease, db: DB) -> Any:
    return await releases.create_releases(get_context(), db, demand_id, body)


@router.get("/demands/{demand_id}/vendor-releases", response_model=list[ReleaseOut])
async def demand_releases(demand_id: uuid.UUID, _: VendorRead, db: DB) -> Any:
    return await releases.list_for_demand(get_context(), db, demand_id)


@router.post("/vendor-releases/{release_id}/{action}", response_model=ReleaseOut)
async def release_action(
    release_id: uuid.UUID, action: Literal["pause", "resume", "withdraw"], _: VendorRelease, db: DB
) -> Any:
    return await releases.change_status(get_context(), db, release_id, action)


# ---------------------------------------------------------------- review queue
@router.get("/vendor-submissions", response_model=list[VendorSubmissionOut])
async def review_queue(
    _: Review,
    db: DB,
    status_: Annotated[str | None, Query(alias="status", max_length=20)] = None,
    demand_id: uuid.UUID | None = None,
    vendor_id: uuid.UUID | None = None,
) -> Any:
    return await submissions.list_internal(
        get_context(), db, status=status_, demand_id=demand_id, vendor_id=vendor_id
    )


@router.get("/vendor-submissions/{submission_id}", response_model=VendorSubmissionOut)
async def review_detail(submission_id: uuid.UUID, _: Review, db: DB) -> Any:
    return await submissions.get_internal(get_context(), db, submission_id)


@router.post("/vendor-submissions/{submission_id}/accept", response_model=VendorSubmissionOut)
async def accept(submission_id: uuid.UUID, body: AcceptIn, _: Review, db: DB) -> Any:
    return await review.accept(get_context(), db, submission_id, body)


@router.post("/vendor-submissions/{submission_id}/reject", response_model=VendorSubmissionOut)
async def reject(submission_id: uuid.UUID, body: RejectIn, _: Review, db: DB) -> Any:
    return await review.reject(get_context(), db, submission_id, body)


@router.post("/vendor-submissions/{submission_id}/mark-duplicate", response_model=VendorSubmissionOut)
async def mark_duplicate(submission_id: uuid.UUID, body: MarkDuplicateIn, _: Review, db: DB) -> Any:
    return await review.mark_duplicate(get_context(), db, submission_id, body)


@router.post("/vendor-submissions/{submission_id}/reopen", response_model=VendorSubmissionOut)
async def reopen(submission_id: uuid.UUID, body: ReopenIn, _: Review, db: DB) -> Any:
    return await review.reopen(get_context(), db, submission_id, body)


# ---------------------------------------------------------------- vendor payable statements
@router.post(
    "/vendor-statements/build", response_model=StatementBuildOut, status_code=status.HTTP_201_CREATED
)
async def build_statements(body: StatementBuildIn, _: PayrollGenerate, db: DB) -> Any:
    return await vendor_statements.build(get_context(), db, body)


@router.get("/vendor-statements", response_model=list[StatementOut])
async def list_statements(
    _: PayrollRead,
    db: DB,
    vendor_id: uuid.UUID | None = None,
    status_: Annotated[Literal["draft", "finalized", "superseded"] | None, Query(alias="status")] = None,
) -> Any:
    return await vendor_statements.list_statements(get_context(), db, vendor_id=vendor_id, status=status_)


@router.get("/vendor-statements/{statement_id}", response_model=StatementDetailOut)
async def get_statement(statement_id: uuid.UUID, _: PayrollRead, db: DB) -> Any:
    return await vendor_statements.get_statement(get_context(), db, statement_id)


@router.post("/vendor-statements/{statement_id}/finalize", response_model=StatementDetailOut)
async def finalize_statement(statement_id: uuid.UUID, _: PayrollFinalize, db: DB) -> Any:
    return await vendor_statements.finalize_statement(get_context(), db, statement_id)


@router.delete("/vendor-statements/{statement_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_statement(statement_id: uuid.UUID, _: PayrollGenerate, db: DB) -> Response:
    await vendor_statements.delete_draft(get_context(), db, statement_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/vendor-statements/{statement_id}/download", response_class=Response)
async def download_statement(
    statement_id: uuid.UUID,
    _: PayrollRead,
    db: DB,
    fmt: Annotated[Literal["pdf", "csv"], Query(alias="format")] = "pdf",
) -> Response:
    return _file(await vendor_statements.download(get_context(), db, statement_id, fmt))
