"""Client Portal BGV status (Epic 20 A6, FR-113; ``/api/v1/client-portal``).

``{status: "clear" | "not_cleared" | null}`` only -- never reasons, checks or evidence -- and only when the
placement's contract shares the BGV outcome (``commercials_service.shares_bgv_outcome``); otherwise ``null``.
A placement of another client is invisible (org RLS) -> 404.
"""

import uuid
from typing import Any

from fastapi import APIRouter

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.dependencies.client import ClientDBSessionDep, ClientDep
from app.schemas.bgv_portal import ClientBgvStatusOut
from app.services.bgv import adjudication

router = APIRouter(
    prefix="/client-portal", tags=["client-portal"], route_class=portal_guard_route("client_user")
)


@router.get("/placements/{placement_id}/bgv-status", response_model=ClientBgvStatusOut)
async def bgv_status(placement_id: uuid.UUID, _: ClientDep, db: ClientDBSessionDep) -> Any:
    return ClientBgvStatusOut(status=await adjudication.client_status(get_context(), db, placement_id))  # type: ignore[arg-type]
