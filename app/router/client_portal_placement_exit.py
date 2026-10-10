"""Client Portal extension and offboarding responses (Epic 31; reconciliation decision 3):
``/api/v1/client-portal/placements``. Client session (org RLS ``client``) + the ``placements:view`` portal
permission; restricted Client Users see only scoped placements. No rates or margin (field-visibility guard).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.core.permissions import Principal
from app.dependencies.client import ClientDBSessionDep, require_portal_permission
from app.schemas.placements_exit_portal import (
    ClientExtensionSummary,
    ClientOffboardingSummary,
    ExtensionRespondIn,
    OffboardingConfirmIn,
)
from app.services.placements import exit_portal

router = APIRouter(
    prefix="/client-portal/placements", tags=["client-portal"], route_class=portal_guard_route("client_user")
)
Viewer = Annotated[Principal, Depends(require_portal_permission("placements:view"))]


def _tenant(principal: Principal) -> uuid.UUID:
    assert principal.tenant_id is not None
    return principal.tenant_id


@router.get("/extensions", response_model=list[ClientExtensionSummary])
async def list_extensions(_: Viewer, db: ClientDBSessionDep) -> Any:
    return await exit_portal.client_extension_summary(get_context(), db)


@router.post("/{placement_id}/extensions/{extension_id}/respond", response_model=ClientExtensionSummary)
async def respond(
    placement_id: uuid.UUID,
    extension_id: uuid.UUID,
    data: ExtensionRespondIn,
    principal: Viewer,
    db: ClientDBSessionDep,
) -> Any:
    return await exit_portal.respond_extension(
        get_context(), db, _tenant(principal), placement_id, extension_id, data
    )


@router.get("/offboarding", response_model=list[ClientOffboardingSummary])
async def list_offboarding(_: Viewer, db: ClientDBSessionDep) -> Any:
    return await exit_portal.client_offboarding_summary(get_context(), db)


@router.post("/{placement_id}/offboarding/confirm", response_model=ClientOffboardingSummary)
async def confirm_offboarding(
    placement_id: uuid.UUID, data: OffboardingConfirmIn, principal: Viewer, db: ClientDBSessionDep
) -> Any:
    return await exit_portal.confirm_offboarding(get_context(), db, _tenant(principal), placement_id, data)
