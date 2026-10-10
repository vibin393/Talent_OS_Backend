"""Vendor Portal content (Epic 17, FR-95 / FR-97 / FR-98): ``/api/v1/vendor-portal`` (Epic 14 owns
``vendor_portal.py`` with ``/me``, auth and users). Every route uses the vendor session (``VendorDep`` +
``VendorDBSessionDep``: org RLS ``vendor``) and calls one ``vendors.portal`` function; another vendor's ids
are
404. Responses are scanned for FR-80 forbidden fields outside production (``portal_guard_route``).
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status
from pydantic import ValidationError

from app.core.context import get_context
from app.core.errors import ValidationFailed
from app.core.field_visibility import portal_guard_route
from app.dependencies.vendor import VendorDBSessionDep, VendorDep
from app.schemas.vendors_portal import (
    PortalDashboardOut,
    PortalDocumentOut,
    PortalPlacementOut,
    PortalReleaseOut,
    PortalScorecardOut,
    PortalStatementOut,
    PortalSubmissionIn,
    PortalSubmissionOut,
)
from app.services import files_service
from app.services.vendors import portal

# ``client_name`` is a ``ClientNameRevealed`` snapshot ("Confidential client" unless revealed, FR-80).
router = APIRouter(
    prefix="/vendor-portal",
    tags=["vendor-portal"],
    route_class=portal_guard_route("vendor_user", allow=("client_name",)),
)
DB = VendorDBSessionDep


def _file(result: files_service.Download) -> Response:
    return Response(
        content=result.data,
        media_type=result.content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(result.filename)}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/dashboard", response_model=PortalDashboardOut)
async def dashboard(principal: VendorDep, db: DB) -> Any:
    return await portal.dashboard(get_context(), db, principal)


@router.get("/releases", response_model=list[PortalReleaseOut])
async def list_releases(principal: VendorDep, db: DB) -> Any:
    return await portal.list_releases(get_context(), db, principal)


@router.get("/releases/{release_id}", response_model=PortalReleaseOut)
async def get_release(release_id: uuid.UUID, principal: VendorDep, db: DB) -> Any:
    return await portal.get_release(get_context(), db, principal, release_id)


@router.post(
    "/releases/{release_id}/submissions",
    response_model=PortalSubmissionOut,
    status_code=status.HTTP_201_CREATED,
)
async def submit(
    release_id: uuid.UUID,
    principal: VendorDep,
    db: DB,
    full_name: Annotated[str, Form(max_length=200)],
    email: Annotated[str, Form(max_length=254)],
    vendor_rate: Annotated[Decimal, Form(ge=0, max_digits=14, decimal_places=4)],
    attestation: Annotated[bool, Form()] = False,
    phone_e164: Annotated[str | None, Form(max_length=20)] = None,
    current_location: Annotated[str | None, Form(max_length=200)] = None,
    total_experience_months: Annotated[int | None, Form(ge=0, le=900)] = None,
    availability_date: Annotated[date | None, Form()] = None,
    notice_period_days: Annotated[int | None, Form(ge=0, le=365)] = None,
    rate_unit: Annotated[str | None, Form(max_length=10)] = None,
    currency: Annotated[str | None, Form(max_length=3)] = None,
    resume: Annotated[UploadFile | None, File()] = None,
) -> Any:
    try:
        data = PortalSubmissionIn(
            full_name=full_name,
            email=email,
            vendor_rate=vendor_rate,
            attestation=attestation,
            phone_e164=phone_e164 or None,
            current_location=current_location or None,
            total_experience_months=total_experience_months,
            availability_date=availability_date,
            notice_period_days=notice_period_days,
            rate_unit=rate_unit or None,
            currency=currency or None,
        )
    except ValidationError as exc:
        raise ValidationFailed(
            "Check the submission fields",
            code="validation_failed",
            details={"errors": exc.errors(include_url=False, include_context=False, include_input=False)},
        ) from exc
    raw = await files_service.read_upload(resume) if resume is not None and resume.filename else None
    return await portal.submit(
        get_context(), db, principal, release_id, data, raw, resume.filename if resume else None
    )


@router.get("/submissions", response_model=list[PortalSubmissionOut])
async def list_submissions(
    principal: VendorDep,
    db: DB,
    release_id: uuid.UUID | None = None,
    label: Annotated[str | None, Query(max_length=60)] = None,
) -> Any:
    return await portal.list_submissions(get_context(), db, principal, release_id=release_id, label=label)


@router.get("/submissions/{submission_id}", response_model=PortalSubmissionOut)
async def get_submission(submission_id: uuid.UUID, principal: VendorDep, db: DB) -> Any:
    return await portal.get_submission(get_context(), db, principal, submission_id)


@router.post("/submissions/{submission_id}/withdraw", response_model=PortalSubmissionOut)
async def withdraw(submission_id: uuid.UUID, principal: VendorDep, db: DB) -> Any:
    return await portal.withdraw(get_context(), db, principal, submission_id)


@router.post("/submissions/{submission_id}/resume", response_model=PortalSubmissionOut)
async def revise_resume(
    submission_id: uuid.UUID, principal: VendorDep, db: DB, resume: Annotated[UploadFile, File()]
) -> Any:
    raw = await files_service.read_upload(resume)
    return await portal.revise_resume(get_context(), db, principal, submission_id, raw, resume.filename)


@router.get("/placements", response_model=list[PortalPlacementOut])
async def list_placements(principal: VendorDep, db: DB) -> Any:
    return await portal.list_placements(get_context(), db, principal)


@router.get("/documents", response_model=list[PortalDocumentOut])
async def list_documents(principal: VendorDep, db: DB) -> Any:
    return await portal.list_documents(get_context(), db, principal)


@router.post("/documents", response_model=PortalDocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    principal: VendorDep,
    db: DB,
    file: Annotated[UploadFile, File()],
    category: Annotated[Literal["agreement", "insurance", "registration", "other"], Form()],
    title: Annotated[str | None, Form(max_length=300)] = None,
    expires_on: Annotated[date | None, Form()] = None,
    document_id: Annotated[uuid.UUID | None, Form()] = None,
) -> Any:
    data = await files_service.read_upload(file)
    return await portal.upload_document(
        get_context(),
        db,
        principal,
        data=data,
        filename=file.filename,
        category=category,
        title=title,
        expires_on=expires_on,
        document_id=document_id,
    )


@router.get("/statements", response_model=list[PortalStatementOut])
async def list_statements(principal: VendorDep, db: DB) -> Any:
    return await portal.list_statements(get_context(), db, principal)


@router.get("/statements/{statement_id}/download", response_class=Response)
async def download_statement(
    statement_id: uuid.UUID,
    principal: VendorDep,
    db: DB,
    fmt: Annotated[Literal["pdf", "csv"], Query(alias="format")] = "pdf",
) -> Response:
    return _file(await portal.statement_download(get_context(), db, principal, statement_id, fmt))


@router.get("/scorecard", response_model=PortalScorecardOut)
async def scorecard(
    principal: VendorDep,
    db: DB,
    period_from: Annotated[date | None, Query(alias="from")] = None,
    period_to: Annotated[date | None, Query(alias="to")] = None,
) -> Any:
    return await portal.scorecard(get_context(), db, principal, period_from, period_to)


@router.get("/files/{file_id}/download", response_class=Response)
async def download_file(file_id: uuid.UUID, principal: VendorDep, db: DB) -> Response:
    return _file(await portal.download_file(get_context(), db, principal, file_id))
