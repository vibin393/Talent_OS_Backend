"""Worker "My Work" BGV and reference checks (Epic 20 A4/A6/A7; ``/api/v1/portal/work/bgv``).

The Candidate principal with the worker org scope (AD-27, R17); no Placement needed. Calls only
``services/bgv/portal.py`` and returns ``schemas/bgv_portal.py`` (no notes, verifier details, reasons or
reference answers). Another candidate's ids -> 404.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, UploadFile

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.dependencies.worker import WorkerDBSessionDep, WorkerDep
from app.schemas.bgv_portal import (
    PortalCaseOut,
    PortalConsentIn,
    PortalConsentView,
    PortalDiscrepancyOut,
    PortalDocumentOut,
    PortalReferenceCheckOut,
    RefereesIn,
    RespondIn,
)
from app.services.bgv import portal
from app.services.files_service import read_upload

router = APIRouter(prefix="/portal/work", tags=["portal-work"], route_class=portal_guard_route("worker"))
Files = Annotated[list[UploadFile] | None, File()]


async def _read(files: list[UploadFile] | None) -> list[tuple[bytes, str | None]]:
    return [(await read_upload(f, 10), f.filename) for f in (files or [])[:5]]


@router.get("/bgv/cases", response_model=list[PortalCaseOut])
async def my_cases(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.list_cases(db, principal.candidate_id)


@router.get("/bgv/cases/{case_id}", response_model=PortalCaseOut)
async def my_case(case_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.get_case(db, principal.candidate_id, case_id)


@router.get("/bgv/cases/{case_id}/consent", response_model=PortalConsentView)
async def consent_view(case_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.consent_view(db, principal.tenant_id, principal.candidate_id, case_id)


@router.post("/bgv/cases/{case_id}/consent", response_model=PortalCaseOut)
async def give_consent(
    case_id: uuid.UUID, data: PortalConsentIn, principal: WorkerDep, db: WorkerDBSessionDep
) -> Any:
    return await portal.give_consent(get_context(), db, principal.candidate_id, case_id, data)


@router.post("/bgv/cases/{case_id}/documents/{key}", response_model=PortalDocumentOut)
async def save_document(
    case_id: uuid.UUID,
    key: str,
    principal: WorkerDep,
    db: WorkerDBSessionDep,
    files: Files = None,
    declaration: Annotated[str | None, Form(max_length=20000)] = None,
    aadhaar_last4: Annotated[str | None, Form(max_length=40)] = None,
) -> Any:
    return await portal.save_document(
        get_context(),
        db,
        principal.candidate_id,
        case_id,
        key,
        uploads=await _read(files),
        declaration=declaration,
        aadhaar_last4=aadhaar_last4,
    )


@router.post("/bgv/cases/{case_id}/submit", response_model=PortalCaseOut)
async def submit_documents(case_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.submit_documents(get_context(), db, principal.candidate_id, case_id)


@router.get("/bgv/discrepancies", response_model=list[PortalDiscrepancyOut])
async def my_discrepancies(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.list_discrepancies(db, principal.candidate_id)


@router.post("/bgv/discrepancies/{discrepancy_id}/respond", response_model=PortalDiscrepancyOut)
async def respond(
    discrepancy_id: uuid.UUID,
    principal: WorkerDep,
    db: WorkerDBSessionDep,
    response: Annotated[str, Form(min_length=5, max_length=4000)],
    files: Files = None,
) -> Any:
    return await portal.respond(
        get_context(),
        db,
        principal.candidate_id,
        discrepancy_id,
        RespondIn(response=response),
        await _read(files),
    )


@router.get("/reference-checks", response_model=list[PortalReferenceCheckOut])
async def my_reference_checks(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.list_reference_checks(db, principal.tenant_id, principal.candidate_id)


@router.post("/reference-checks/{reference_check_id}/referees", response_model=PortalReferenceCheckOut)
async def add_referees(
    reference_check_id: uuid.UUID, data: RefereesIn, principal: WorkerDep, db: WorkerDBSessionDep
) -> Any:
    return await portal.add_referees(
        get_context(), db, principal.tenant_id, principal.candidate_id, reference_check_id, data
    )
