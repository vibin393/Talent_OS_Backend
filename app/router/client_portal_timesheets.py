"""Client Portal timesheets (Epic 25; reconciliation decision: this epic owns list / detail / decide / bulk):
``/api/v1/client-portal/timesheets``. Client session (org RLS ``client``) + the ``time:approve`` portal
permission; restricted Client Users see only scoped Placements. Decisions always go through
``approvals_service.decide`` with the Client ``ActorRef``. Hours only (FR-80; field-visibility guard).
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request

from app.core.context import get_context
from app.core.errors import ValidationFailed
from app.core.field_visibility import portal_guard_route
from app.core.idempotency import HEADER
from app.core.permissions import Principal
from app.dependencies.client import ClientDBSessionDep, require_portal_permission
from app.schemas.time_portal import (
    ClientBulkDecideIn,
    ClientBulkDecideOut,
    ClientBulkPreviewIn,
    ClientBulkPreviewOut,
    ClientDecideIn,
    ClientTimesheetDetail,
    ClientTimesheetSummary,
)
from app.services.time import portal

router = APIRouter(
    prefix="/client-portal/timesheets", tags=["client-portal"], route_class=portal_guard_route("client_user")
)
TimeApprover = Annotated[Principal, Depends(require_portal_permission("time:approve"))]


def _tenant(principal: Principal) -> uuid.UUID:
    assert principal.tenant_id is not None
    return principal.tenant_id


@router.get("", response_model=list[ClientTimesheetSummary])
async def list_timesheets(
    principal: TimeApprover,
    db: ClientDBSessionDep,
    status: Annotated[Literal["pending", "all"], Query()] = "pending",
) -> Any:
    return await portal.client_list(get_context(), db, _tenant(principal), status=status)


@router.post("/bulk-preview", response_model=ClientBulkPreviewOut)
async def bulk_preview(data: ClientBulkPreviewIn, principal: TimeApprover, db: ClientDBSessionDep) -> Any:
    return await portal.bulk_preview(get_context(), db, _tenant(principal), data.timesheet_ids)


@router.post("/bulk-decide", response_model=ClientBulkDecideOut)
async def bulk_decide(
    request: Request, data: ClientBulkDecideIn, principal: TimeApprover, db: ClientDBSessionDep
) -> Any:
    if not (request.headers.get(HEADER) or "").strip():
        raise ValidationFailed("The Idempotency-Key header is required", code="idempotency_key_required")
    return await portal.bulk_decide(
        get_context(),
        db,
        _tenant(principal),
        data.timesheet_ids,
        data.action,
        data.comment,
        data.preview_hash,
    )


@router.get("/{timesheet_id}", response_model=ClientTimesheetDetail)
async def get_timesheet(timesheet_id: uuid.UUID, principal: TimeApprover, db: ClientDBSessionDep) -> Any:
    return await portal.client_detail(get_context(), db, _tenant(principal), timesheet_id)


@router.post("/{timesheet_id}/decide", response_model=ClientTimesheetDetail)
async def decide(
    timesheet_id: uuid.UUID, data: ClientDecideIn, principal: TimeApprover, db: ClientDBSessionDep
) -> Any:
    return await portal.client_decide(
        get_context(), db, _tenant(principal), timesheet_id, data.action, data.comment
    )
