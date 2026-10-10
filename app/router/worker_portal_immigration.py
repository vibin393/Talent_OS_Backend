"""Worker "My Work" immigration (Epic 46, ``/api/v1/portal/work/immigration``): the Worker's own Cases (plain
status, requested documents), the ``immigration`` notice + consent, and uploads into P2 documents. Worker org
scope + explicit ownership: another Worker's case or request is a 404 (AD-26)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, UploadFile

from app.dependencies.worker import WorkerDBSessionDep, WorkerDep
from app.schemas.immigration_portal import PortalCaseDetailOut, PortalCasesOut, PortalRequestOut
from app.schemas.pipeline import PublicActionOut
from app.services import files_service
from app.services.immigration import checklist, portal

router = APIRouter(prefix="/portal/work/immigration", tags=["portal-work"])


@router.get("", response_model=PortalCasesOut)
async def my_cases(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.list_mine(db, principal.candidate_id)


@router.post("/consent", response_model=PublicActionOut)
async def give_consent(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return PublicActionOut(status=await portal.give_consent(db, principal.candidate_id))


@router.post("/requests/{request_id}/upload", response_model=PortalRequestOut)
async def upload(
    request_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep, file: Annotated[UploadFile, File()]
) -> Any:
    data = await files_service.read_upload(file, checklist.MAX_UPLOAD_MB)
    return await portal.upload(db, principal.candidate_id, request_id, data, file.filename)


@router.get("/{case_id}", response_model=PortalCaseDetailOut)
async def my_case(case_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.detail(db, principal.candidate_id, case_id)
