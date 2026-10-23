# ruff: file-ignore[line-too-long]
"""Calendar Sync API (Epic 72, spec A9), prefix ``/api/v1/calendar-sync``.

| Method + path | Permission |
|---|---|
| ``GET /settings`` · ``PUT /settings`` | ``integration:view`` / ``integration:manage`` (human) |
| ``GET /me`` · ``POST /me/resync`` (3 / hour) · ``PUT /me/free-busy-optout`` | ``connection:manage_own`` |
| ``GET /interviews/{id}/sync`` | ``interview:read`` |
| ``POST /interviews/{id}/recreate-event`` · ``POST /interviews/{id}/keep-talent-os-time`` | ``interview:schedule``, human |
| ``POST /interviews/{id}/dismiss-change`` | ``interview:schedule`` |
| ``GET /channels?status=`` | ``integration:view`` (user names only) |
"""

import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.errors import ConflictError
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.calendar_sync import (
    ActionOut,
    CalendarSyncSettingsIn,
    CalendarSyncSettingsOut,
    ChannelAdminOut,
    InterviewSyncOut,
    MySyncOut,
    OptOutIn,
)
from app.services import calendar_sync_service as svc

router = APIRouter(prefix="/calendar-sync", tags=["calendar-sync"])
DB = TenantDBSessionDep
Manage = Annotated[Principal, Depends(require("integration:manage"))]
View = Annotated[Principal, Depends(require_any("integration:view", "integration:manage"))]
Own = Annotated[Principal, Depends(require("connection:manage_own"))]
ReadInterview = Annotated[Principal, Depends(require("interview:read"))]
Schedule = Annotated[Principal, Depends(require("interview:schedule"))]
_resyncs: dict[uuid.UUID, list[float]] = {}


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


@router.get("/settings", response_model=CalendarSyncSettingsOut)
async def get_settings(_: View, db: DB) -> Any:
    return await svc.get_settings(get_context(), db)


@router.put("/settings", response_model=CalendarSyncSettingsOut)
async def update_settings(body: CalendarSyncSettingsIn, _: Manage, db: DB) -> Any:
    _human()
    return await svc.update_settings(get_context(), db, body)


@router.get("/me", response_model=MySyncOut)
async def me(_: Own, db: DB) -> Any:
    return await svc.mine(get_context(), db)


@router.post("/me/resync", response_model=ActionOut)
async def resync(principal: Own, db: DB) -> Any:
    now = time.monotonic()
    recent = [t for t in _resyncs.get(principal.id, []) if t > now - 3600]
    if len(recent) >= 3:
        raise ConflictError("You can sync up to 3 times an hour", code="resync_rate_limited")
    _resyncs[principal.id] = [*recent, now]
    count = await svc.resync_mine(get_context(), db)
    return ActionOut(status=f"synced:{count}")


@router.put("/me/free-busy-optout", response_model=MySyncOut)
async def optout(body: OptOutIn, _: Own, db: DB) -> Any:
    return await svc.set_optout(get_context(), db, body.opted_out)


@router.get("/interviews/{interview_id}/sync", response_model=InterviewSyncOut)
async def interview_sync(interview_id: uuid.UUID, _: ReadInterview, db: DB) -> Any:
    return await svc.interview_sync(get_context(), db, interview_id)


@router.post("/interviews/{interview_id}/recreate-event", response_model=ActionOut)
async def recreate(interview_id: uuid.UUID, _: Schedule, db: DB) -> Any:
    _human()
    return ActionOut(status=await svc.recreate_event(get_context(), db, interview_id))


@router.post("/interviews/{interview_id}/keep-talent-os-time", response_model=ActionOut)
async def keep_time(interview_id: uuid.UUID, _: Schedule, db: DB) -> Any:
    _human()
    return ActionOut(status=await svc.keep_talent_os_time(get_context(), db, interview_id))


@router.post("/interviews/{interview_id}/dismiss-change", response_model=ActionOut)
async def dismiss(interview_id: uuid.UUID, _: Schedule, db: DB) -> Any:
    return ActionOut(status=await svc.dismiss_change(get_context(), db, interview_id))


@router.get("/channels", response_model=list[ChannelAdminOut])
async def channels(_: View, db: DB, status: str | None = None) -> Any:
    return await svc.list_channels(get_context(), db, status)
