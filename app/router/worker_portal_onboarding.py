# ruff: file-ignore[line-too-long]
"""Worker "My Work" onboarding (Epic 23 A7, ``/api/v1/portal/work/onboarding``). Worker dependency + worker org
scope; calls only ``services/onboarding/portal.py``. Another Worker's case or task, and internal tasks, are 404."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, Request, Response, UploadFile, status

from app.core.context import get_context
from app.dependencies.worker import WorkerDBSessionDep, WorkerDep
from app.schemas.onboarding_portal import (
    AcknowledgeIn,
    DraftIn,
    PortalCaseOut,
    PortalCaseSummary,
    PortalSignatureRefOut,
    PortalTaskOut,
    SubmitIn,
)
from app.services.onboarding import portal

router = APIRouter(prefix="/portal/work/onboarding", tags=["portal-work"])
MAX_UPLOAD = 10 * 1024 * 1024


@router.get("/cases", response_model=list[PortalCaseSummary])
async def my_cases(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.list_cases(db, principal.candidate_id)


@router.get("/cases/{case_id}", response_model=PortalCaseOut)
async def my_case(case_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.get_case(db, principal.tenant_id, principal.candidate_id, case_id)


@router.patch("/tasks/{task_id}/draft", status_code=status.HTTP_204_NO_CONTENT)
async def save_draft(
    task_id: uuid.UUID, body: DraftIn, principal: WorkerDep, db: WorkerDBSessionDep
) -> Response:
    await portal.save_draft(db, principal.tenant_id, principal.candidate_id, task_id, body.draft)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/tasks/{task_id}/submit", response_model=PortalTaskOut)
async def submit(task_id: uuid.UUID, body: SubmitIn, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.submit(db, principal.tenant_id, principal.candidate_id, task_id, body.payload)


@router.post("/tasks/{task_id}/files", status_code=status.HTTP_201_CREATED)
async def upload(
    task_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep, file: Annotated[UploadFile, File()]
) -> Any:
    data = await file.read(MAX_UPLOAD + 1)
    doc_id = await portal.upload(
        db, principal.tenant_id, principal.candidate_id, task_id, data, file.filename
    )
    return {"document_id": str(doc_id)}


@router.get("/tasks/{task_id}/policy", response_class=Response)
async def policy(task_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Response:
    pdf = await portal.policy_pdf(db, principal.tenant_id, principal.candidate_id, task_id)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'inline; filename="policy.pdf"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/tasks/{task_id}/acknowledge", status_code=status.HTTP_204_NO_CONTENT)
async def acknowledge(
    task_id: uuid.UUID, body: AcknowledgeIn, request: Request, principal: WorkerDep, db: WorkerDBSessionDep
) -> Response:
    await portal.acknowledge(
        db,
        principal.tenant_id,
        principal.candidate_id,
        task_id,
        accepted=body.accepted,
        ip=get_context().ip,
        user_agent=request.headers.get("user-agent"),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/tasks/{task_id}/signature", response_model=PortalSignatureRefOut)
async def signature(task_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.signature(db, principal.candidate_id, task_id)
