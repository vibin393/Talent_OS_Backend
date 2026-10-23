# ruff: file-ignore[line-too-long]
"""Ecosystem controls (Epic 66, spec 0.18): state, the Ecosystem Kill Switch, the app policy, MCP enablement storage and
usage meters. Every mutation is human-only (403 ``human_only_action`` for API keys and automated actors)."""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.errors import ValidationFailed
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.ecosystem import AppPolicyIn, EcosystemStateOut, KillSwitchIn, McpEnableIn
from app.schemas.metering import UsageOut, UsageQuery
from app.services import ecosystem_service, metering_service

router = APIRouter(prefix="/ecosystem", tags=["ecosystem"])
DB = TenantDBSessionDep
View = Annotated[Principal, Depends(require("integration:view"))]
Manage = Annotated[Principal, Depends(require("integration:manage"))]
Kill = Annotated[Principal, Depends(require("ecosystem:kill_switch"))]
Configure = Annotated[Principal, Depends(require("ai:agent_configure"))]
Usage = Annotated[Principal, Depends(require("usage:read"))]


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


@router.get("", response_model=EcosystemStateOut)
async def state(_: View, db: DB) -> EcosystemStateOut:
    return await ecosystem_service.state(get_context(), db)


@router.post("/kill-switch", response_model=EcosystemStateOut)
async def kill_switch(body: KillSwitchIn, _: Kill, db: DB) -> EcosystemStateOut:
    _human()
    return await ecosystem_service.set_kill_switch(get_context(), db, on=body.on, reason=body.reason)


@router.put("/app-policy", response_model=EcosystemStateOut)
async def app_policy(body: AppPolicyIn, _: Manage, db: DB) -> EcosystemStateOut:
    _human()
    return await ecosystem_service.set_app_policy(get_context(), db, body)


@router.put("/mcp", response_model=EcosystemStateOut)
async def mcp_enabled(body: McpEnableIn, _: Configure, db: DB) -> EcosystemStateOut:
    _human()
    return await ecosystem_service.set_mcp_enabled(
        get_context(), db, on=body.on, notice_version=body.notice_version, tool_groups=body.tool_groups
    )


@router.get("/usage", response_model=UsageOut)
async def usage(
    _: Usage,
    db: DB,
    date_from: Annotated[dt.date, Query(alias="from")],
    date_to: Annotated[dt.date, Query(alias="to")],
    unit: Annotated[list[str] | None, Query(max_length=30)] = None,
    installation_id: uuid.UUID | None = None,
) -> UsageOut:
    try:
        q = UsageQuery(date_from=date_from, date_to=date_to, units=unit, installation_id=installation_id)
    except ValueError as exc:
        raise ValidationFailed("Invalid usage query", details={"error": str(exc)[:300]}) from exc
    return await metering_service.usage(get_context(), db, q)
