# ruff: noqa: E501
"""MCP administration and self-service (Epic 73, spec A4): ``/api/v1/mcp/*``. People only: API keys and App tokens are
refused (403 ``human_only_action``); every mutation is audited."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.core.context import get_context
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import CurrentUserDep as CurrentUser
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.mcp import (
    McpDisableIn,
    McpEnableIn,
    McpEndSessionIn,
    McpHoldOut,
    McpHoldReleaseIn,
    McpMeOut,
    McpSessionOut,
    McpSessionPage,
    McpSessionQuery,
    McpSettingsIn,
    McpSettingsOut,
    McpToolOut,
)
from app.services import mcp_service as svc

router = APIRouter(prefix="/mcp", tags=["mcp"])
DB = TenantDBSessionDep
View = Annotated[Principal, Depends(require_any("ai:agent_configure", "ai:govern"))]
Configure = Annotated[Principal, Depends(require("ai:agent_configure"))]
Catalogue = Annotated[Principal, Depends(require_any("ai:agent_configure", "ai:govern", "mcp:use"))]


@router.get("/settings", response_model=McpSettingsOut)
async def get_settings(_: View, db: DB) -> McpSettingsOut:
    return await svc.get_settings_out(get_context(), db)


@router.post("/enable", response_model=McpSettingsOut)
async def enable(body: McpEnableIn, _: Configure, db: DB) -> McpSettingsOut:
    return await svc.enable(get_context(), db, body)


@router.post("/disable", response_model=McpSettingsOut)
async def disable(body: McpDisableIn, _: Configure, db: DB) -> McpSettingsOut:
    return await svc.disable(get_context(), db, reason=body.reason)


@router.patch("/settings", response_model=McpSettingsOut)
async def update_settings(body: McpSettingsIn, _: Configure, db: DB) -> McpSettingsOut:
    return await svc.update_settings(get_context(), db, body)


@router.get("/tools", response_model=list[McpToolOut])
async def tools(_: Catalogue, db: DB) -> list[McpToolOut]:
    return await svc.tool_catalogue(get_context(), db)


@router.get("/me", response_model=McpMeOut)
async def me(_: CurrentUser, db: DB) -> McpMeOut:
    return await svc.me(get_context(), db)


@router.get("/sessions", response_model=McpSessionPage)
async def sessions(
    _: CurrentUser,
    db: DB,
    status: Literal["active", "paused", "ended"] | None = None,
    user_id: uuid.UUID | None = None,
    installation_id: uuid.UUID | None = None,
    started_from: datetime | None = None,
    started_to: datetime | None = None,
    mine: bool = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> McpSessionPage:
    q = McpSessionQuery(
        status=status, user_id=user_id, installation_id=installation_id, started_from=started_from,
        started_to=started_to, mine=mine, page=page, page_size=page_size,
    )  # fmt: skip
    return await svc.list_sessions(get_context(), db, q)


@router.post("/sessions/{session_id}/end", response_model=McpSessionOut)
async def end_session(session_id: uuid.UUID, body: McpEndSessionIn, _: CurrentUser, db: DB) -> McpSessionOut:
    return await svc.end_session(get_context(), db, session_id, reason=body.reason)


@router.get("/holds", response_model=list[McpHoldOut])
async def holds(_: Configure, db: DB) -> list[McpHoldOut]:
    return await svc.list_holds(get_context(), db)


@router.post("/holds/{hold_id}/release", response_model=McpHoldOut)
async def release_hold(hold_id: uuid.UUID, body: McpHoldReleaseIn, _: Configure, db: DB) -> McpHoldOut:
    return await svc.release_hold(get_context(), db, hold_id, note=body.note)
