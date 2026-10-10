"""Client Portal expenses (Epic 27): ``/api/v1/client-portal/expenses`` -- list, detail, decide (per-line) and
receipt preview. Client session (org RLS ``client``) + the ``time:approve`` portal permission (the expense
route is the Placement's timesheet route); restricted Client Users see only scoped Placements.
Decisions always
go through ``approvals_service.decide`` with the Client ``ActorRef``. Billable lines only; never markup %,
non-billable lines or Worker reimbursement totals (field-visibility guard).
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.core.permissions import Principal
from app.dependencies.client import ClientDBSessionDep, require_portal_permission
from app.router.expenses import file_response
from app.schemas.expenses_portal import ClientClaimDecideIn, ClientClaimDetail, ClientClaimSummary
from app.services.expenses import portal

router = APIRouter(
    prefix="/client-portal/expenses", tags=["client-portal"], route_class=portal_guard_route("client_user")
)
Approver = Annotated[Principal, Depends(require_portal_permission("time:approve"))]


def _tenant(principal: Principal) -> uuid.UUID:
    assert principal.tenant_id is not None
    return principal.tenant_id


@router.get("", response_model=list[ClientClaimSummary])
async def list_claims(
    principal: Approver,
    db: ClientDBSessionDep,
    status: Annotated[Literal["pending", "all"], Query()] = "pending",
) -> Any:
    return await portal.client_list(get_context(), db, _tenant(principal), status=status)


@router.get("/{claim_id}", response_model=ClientClaimDetail)
async def get_claim(claim_id: uuid.UUID, principal: Approver, db: ClientDBSessionDep) -> Any:
    return await portal.client_detail(get_context(), db, _tenant(principal), claim_id)


@router.post("/{claim_id}/decide", response_model=ClientClaimDetail)
async def decide(
    claim_id: uuid.UUID, data: ClientClaimDecideIn, principal: Approver, db: ClientDBSessionDep
) -> Any:
    return await portal.client_decide(
        get_context(),
        db,
        _tenant(principal),
        claim_id,
        data.action,
        data.comment,
        [d.model_dump(mode="json") for d in data.line_decisions],
    )


@router.get(
    "/{claim_id}/lines/{line_id}/receipt",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def line_receipt(
    claim_id: uuid.UUID, line_id: uuid.UUID, principal: Approver, db: ClientDBSessionDep
) -> Response:
    data, name, ctype = await portal.client_receipt(get_context(), db, _tenant(principal), claim_id, line_id)
    return file_response(data, name, ctype)
