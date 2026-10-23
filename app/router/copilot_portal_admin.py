# ruff: file-ignore[line-too-long]
"""Portal Copilot administration (Epic 77 A5): ``/api/v1/copilot/portal-admin`` (internal users). Settings read needs
``ai:govern`` or ``ai:agent_configure``; ``PUT /settings`` needs ``ai:agent_configure`` and a human (API keys and Apps
are refused); usage needs ``ai:govern`` and carries metadata only (never conversation text)."""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.portal_copilot import PortalCopilotSettingsIn, PortalCopilotSettingsOut, PortalUsageOut
from app.services import portal_copilot_service as svc

router = APIRouter(prefix="/copilot/portal-admin", tags=["copilot"])
DB = TenantDBSessionDep
SettingsRead = Annotated[Principal, Depends(require_any("ai:govern", "ai:agent_configure"))]
Configure = Annotated[Principal, Depends(require("ai:agent_configure"))]
Govern = Annotated[Principal, Depends(require("ai:govern"))]


@router.get("/settings", response_model=PortalCopilotSettingsOut)
async def get_settings(_: SettingsRead, db: DB) -> PortalCopilotSettingsOut:
    return await svc.get_settings(get_context(), db)


@router.put("/settings", response_model=PortalCopilotSettingsOut)
async def update_settings(body: PortalCopilotSettingsIn, _: Configure, db: DB) -> PortalCopilotSettingsOut:
    require_human(actor_from_ctx(get_context()))
    return await svc.update_settings(get_context(), db, body)


@router.get("/usage", response_model=PortalUsageOut)
async def usage(
    _: Govern,
    db: DB,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    portal: Annotated[Literal["client", "vendor", "candidate"] | None, Query()] = None,
) -> PortalUsageOut:
    return await svc.usage(get_context(), db, date_from=date_from, date_to=date_to, portal=portal)
