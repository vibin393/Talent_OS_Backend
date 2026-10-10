# ruff: noqa: E501
"""AI campaign copy (Epic 78, FR-401..FR-403): ``/api/v1/campaign-copy/*``.

Permissions: ``campaign:read`` (reads), ``campaign:write`` / ``sequence:write`` by target (request, cancel, edit,
discard, apply -- human only), ``campaign:copy_approve`` (opened, acknowledge, ★ approve -- human only). API keys can
use the GETs only (every write refuses automated actors with 403 ``human_only_action``). Not exposed to Apps / MCP.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.campaign_copy import (
    ApplyIn,
    ApplyOut,
    ApproveIn,
    CopyDraftOut,
    CopyRequestDetailOut,
    CopyRequestIn,
    CopyRequestOut,
    CopyStatusOut,
    DraftEditIn,
)
from app.services import campaign_copy_service as svc

router = APIRouter(prefix="/campaign-copy", tags=["campaign-copy"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require("campaign:read"))]
Approve = Annotated[Principal, Depends(require("campaign:copy_approve"))]


@router.post("/requests", response_model=CopyRequestOut, status_code=status.HTTP_202_ACCEPTED)
async def request_copy(
    data: CopyRequestIn, _: Read, db: DB, idem: Annotated[IdempotencyGuard, Depends(idempotency())]
) -> Any:
    return await idem.run(lambda: svc.request_copy(get_context(), db, data))


@router.get("/requests", response_model=list[CopyRequestOut])
async def list_requests(
    _: Read,
    db: DB,
    campaign_id: Annotated[uuid.UUID | None, Query()] = None,
    sequence_step_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Any:
    return await svc.list_requests(get_context(), db, campaign_id=campaign_id, sequence_step_id=sequence_step_id)


@router.get("/requests/{request_id}", response_model=CopyRequestDetailOut)
async def get_request(request_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.get_request(get_context(), db, request_id)


@router.post("/requests/{request_id}/cancel", response_model=CopyRequestOut)
async def cancel(request_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.cancel(get_context(), db, request_id)


@router.post("/requests/{request_id}/segments/{segment_key}/opened", status_code=status.HTTP_204_NO_CONTENT)
async def segment_opened(request_id: uuid.UUID, segment_key: str, _: Approve, db: DB) -> Response:
    await svc.mark_segment_opened(get_context(), db, request_id, segment_key[:40])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/drafts/{draft_id}/opened", status_code=status.HTTP_204_NO_CONTENT)
async def opened(draft_id: uuid.UUID, _: Approve, db: DB) -> Response:
    await svc.mark_opened(get_context(), db, draft_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/drafts/{draft_id}", response_model=CopyDraftOut)
async def edit_draft(draft_id: uuid.UUID, data: DraftEditIn, _: Read, db: DB) -> Any:
    return await svc.edit_draft(get_context(), db, draft_id, data)


@router.post("/drafts/{draft_id}/acknowledge-warnings", response_model=CopyDraftOut)
async def acknowledge(draft_id: uuid.UUID, _: Approve, db: DB) -> Any:
    return await svc.acknowledge_warnings(get_context(), db, draft_id)


@router.post("/drafts/{draft_id}/approve", response_model=CopyDraftOut)
async def approve(draft_id: uuid.UUID, data: ApproveIn, _: Approve, db: DB) -> Any:
    return await svc.approve(get_context(), db, draft_id, data)


@router.post("/drafts/{draft_id}/discard", response_model=CopyDraftOut)
async def discard(draft_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.discard(get_context(), db, draft_id)


@router.post("/apply", response_model=ApplyOut)
async def apply(data: ApplyIn, _: Read, db: DB) -> Any:
    return await svc.apply(get_context(), db, data)


@router.get("/campaigns/{campaign_id}/status", response_model=CopyStatusOut)
async def campaign_status(campaign_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.campaign_status(get_context(), db, campaign_id)
