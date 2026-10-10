"""Worker "My Work" expenses (Epic 27): ``/api/v1/portal/work/expense-claims`` and
``/api/v1/portal/work/expense-receipts``.

The Candidate principal with the worker org scope (AD-27); calls only
``services/expenses/portal.py`` and returns
``schemas/expenses_portal.py`` (the Worker's own costs: no bill amount or markup; scanned outside
production by the
field-visibility guard). Another Worker's ids -> 404.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, UploadFile, status
from fastapi.responses import Response

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.dependencies.worker import WorkerDBSessionDep, WorkerDep
from app.router.expenses import file_response
from app.schemas.expenses import LineIn, LinePreviewOut
from app.schemas.expenses_portal import (
    ReceiptOut,
    ReceiptReadOut,
    WorkerClaimablePlacement,
    WorkerClaimIn,
    WorkerClaimOut,
    WorkerClaimSummary,
    WorkerClaimUpdate,
    WorkerExpenseSettings,
)
from app.services.expenses import portal

router = APIRouter(prefix="/portal/work", tags=["portal-work"], route_class=portal_guard_route("worker"))
CLAIMS = "/expense-claims"


@router.get("/expense-settings", response_model=WorkerExpenseSettings)
async def settings(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.worker_settings(get_context(), db, principal.tenant_id)


@router.get(CLAIMS, response_model=list[WorkerClaimSummary])
async def my_claims(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.my_claims(get_context(), db, principal.tenant_id, principal.candidate_id)


@router.get(f"{CLAIMS}/placements", response_model=list[WorkerClaimablePlacement])
async def claimable_placements(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.claimable_placements(get_context(), db, principal.tenant_id, principal.candidate_id)


@router.post(CLAIMS, response_model=WorkerClaimOut, status_code=status.HTTP_201_CREATED)
async def create_claim(data: WorkerClaimIn, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.create(
        get_context(), db, principal.tenant_id, principal.candidate_id, data.placement_id, data.title
    )


@router.get(CLAIMS + "/{claim_id}", response_model=WorkerClaimOut)
async def my_claim(claim_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.my_claim(get_context(), db, principal.tenant_id, principal.candidate_id, claim_id)


@router.patch(CLAIMS + "/{claim_id}", response_model=WorkerClaimOut)
async def update_claim(
    claim_id: uuid.UUID, data: WorkerClaimUpdate, principal: WorkerDep, db: WorkerDBSessionDep
) -> Any:
    return await portal.worker_action(
        get_context(), db, principal.tenant_id, principal.candidate_id, claim_id, "title", title=data.title
    )


@router.post(CLAIMS + "/{claim_id}/lines/preview", response_model=LinePreviewOut)
async def preview_line(
    claim_id: uuid.UUID, data: LineIn, principal: WorkerDep, db: WorkerDBSessionDep
) -> Any:
    return await portal.preview(
        get_context(), db, principal.tenant_id, principal.candidate_id, claim_id, data
    )


@router.post(CLAIMS + "/{claim_id}/lines", response_model=WorkerClaimOut)
async def add_line(claim_id: uuid.UUID, data: LineIn, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.worker_action(
        get_context(), db, principal.tenant_id, principal.candidate_id, claim_id, "upsert", data=data
    )


@router.put(CLAIMS + "/{claim_id}/lines/{line_id}", response_model=WorkerClaimOut)
async def update_line(
    claim_id: uuid.UUID, line_id: uuid.UUID, data: LineIn, principal: WorkerDep, db: WorkerDBSessionDep
) -> Any:
    return await portal.worker_action(
        get_context(),
        db,
        principal.tenant_id,
        principal.candidate_id,
        claim_id,
        "upsert",
        data=data,
        line_id=line_id,
    )


@router.delete(CLAIMS + "/{claim_id}/lines/{line_id}", response_model=WorkerClaimOut)
async def delete_line(
    claim_id: uuid.UUID, line_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep
) -> Any:
    return await portal.worker_action(
        get_context(), db, principal.tenant_id, principal.candidate_id, claim_id, "delete", line_id=line_id
    )


@router.post(CLAIMS + "/{claim_id}/submit", response_model=WorkerClaimOut)
async def submit(claim_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.worker_action(
        get_context(), db, principal.tenant_id, principal.candidate_id, claim_id, "submit"
    )


@router.post(CLAIMS + "/{claim_id}/recall", response_model=WorkerClaimOut)
async def recall(claim_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.worker_action(
        get_context(), db, principal.tenant_id, principal.candidate_id, claim_id, "recall"
    )


@router.post("/expense-receipts", response_model=ReceiptOut, status_code=status.HTTP_201_CREATED)
async def upload_receipt(
    file: Annotated[UploadFile, File()], principal: WorkerDep, db: WorkerDBSessionDep
) -> Any:
    from app.services import files_service

    data = await files_service.read_upload(file, 50)
    return await portal.upload_receipt(
        get_context(), db, principal.tenant_id, principal.candidate_id, data, file.filename
    )


@router.post("/expense-receipts/{file_id}/read", response_model=ReceiptReadOut)
async def read_receipt(file_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.read_receipt(get_context(), db, principal.tenant_id, principal.candidate_id, file_id)


@router.get(
    "/expense-receipts/{file_id}",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def download_receipt(file_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Response:
    data, name, ctype = await portal.download_receipt(
        get_context(), db, principal.tenant_id, principal.candidate_id, file_id
    )
    return file_response(data, name, ctype)
