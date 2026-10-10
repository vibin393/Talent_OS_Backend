# ruff: noqa: E501
"""Scheduling Agent, internal API (Epic 59 A6).

Permissions: ``interview:schedule`` (start / cancel requests), ``interview:read`` (reads), a participant of the
interview or ``interview:schedule`` (reschedule requests), the signed-in user (own preferences), ``interview:configure``
(another user's preferences), ``ai:agent_configure`` (settings). Starting a request is human only (403
``human_only_action``)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.errors import NotFound
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.scheduling_agent import (
    CancelRequestIn,
    RequestDetailOut,
    RequestOptionsOut,
    RequestOut,
    RequestStartIn,
    RescheduleRequestIn,
    SchedulingPrefsIn,
    SchedulingPrefsOut,
    SchedulingSettingsIn,
    SchedulingSettingsOut,
)
from app.services import scheduling_agent_service as svc

router = APIRouter(tags=["scheduling-agent"])
DB = TenantDBSessionDep
Schedule = Annotated[Principal, Depends(require("interview:schedule"))]
Read = Annotated[Principal, Depends(require("interview:read"))]
Configure = Annotated[Principal, Depends(require("ai:agent_configure"))]
InterviewConfigure = Annotated[Principal, Depends(require("interview:configure"))]


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


def _user(principal: Any) -> uuid.UUID:
    if not isinstance(principal, Principal) or principal.type != "user":
        raise NotFound("Resource not found")
    return principal.id


# ================================================================ requests
@router.get("/applications/{application_id}/scheduling-options", response_model=RequestOptionsOut)
async def options(application_id: uuid.UUID, stage_id: Annotated[uuid.UUID, Query()], _: Read, db: DB) -> Any:
    return await svc.requests.options(get_context(), db, application_id, stage_id)


@router.post(
    "/applications/{application_id}/scheduling-requests",
    response_model=RequestDetailOut,
    status_code=status.HTTP_201_CREATED,
)
async def start(
    application_id: uuid.UUID,
    body: RequestStartIn,
    _: Schedule,
    db: DB,
    idem: Annotated[IdempotencyGuard, Depends(idempotency())],
) -> Any:
    _human()
    ctx = get_context()

    async def _run() -> RequestDetailOut:
        req = await svc.requests.start(ctx, db, application_id, body)
        return await svc.settings_api.request_detail(db, req)

    return await idem.run(_run)


@router.get("/applications/{application_id}/scheduling-requests", response_model=list[RequestOut])
async def list_for_application(application_id: uuid.UUID, _: Read, db: DB) -> Any:
    rows = await svc.requests.list_for_application(get_context(), db, application_id)
    return [await svc.settings_api.request_out(db, r) for r in rows]


@router.get("/scheduling-requests/{request_id}", response_model=RequestDetailOut)
async def get_request(request_id: uuid.UUID, _: Read, db: DB) -> Any:
    req = await svc.requests.readable(get_context(), db, request_id)
    return await svc.settings_api.request_detail(db, req)


@router.post("/scheduling-requests/{request_id}/cancel", response_model=RequestDetailOut)
async def cancel_request(request_id: uuid.UUID, body: CancelRequestIn, _: Schedule, db: DB) -> Any:
    _human()
    req = await svc.requests.cancel(get_context(), db, request_id, body.reason)
    return await svc.settings_api.request_detail(db, req)


@router.post(
    "/interviews/{interview_id}/reschedule-requests",
    response_model=RequestOut,
    status_code=status.HTTP_201_CREATED,
)
async def request_reschedule(interview_id: uuid.UUID, body: RescheduleRequestIn, db: DB) -> Any:
    ctx = get_context()
    _human()
    _user(ctx.principal)
    req = await svc.requests.request_reschedule(ctx, db, interview_id, by="interviewer", reason=body.reason)
    return await svc.settings_api.request_out(db, req)


@router.get("/interviews/{interview_id}/scheduling-requests", response_model=list[RequestOut])
async def list_for_interview(interview_id: uuid.UUID, _: Read, db: DB) -> Any:
    from app.services import interviews_service

    ctx = get_context()
    interview = await interviews_service.get_interview(ctx, db, interview_id)
    rows = await svc.requests.list_for_application(ctx, db, interview.application_id)
    return [
        await svc.settings_api.request_out(db, r)
        for r in rows
        if r.interview_id == interview_id or r.booked_interview_id == interview_id
    ]


# ================================================================ preferences
@router.get("/me/scheduling-preferences", response_model=SchedulingPrefsOut)
async def my_prefs(db: DB) -> Any:
    return await svc.settings_api.get_prefs(db, _user(get_context().principal))


@router.put("/me/scheduling-preferences", response_model=SchedulingPrefsOut)
async def put_my_prefs(body: SchedulingPrefsIn, db: DB) -> Any:
    ctx = get_context()
    return await svc.settings_api.put_prefs(ctx, db, _user(ctx.principal), body)


@router.get("/users/{user_id}/scheduling-preferences", response_model=SchedulingPrefsOut)
async def user_prefs(user_id: uuid.UUID, _: InterviewConfigure, db: DB) -> Any:
    return await svc.settings_api.get_prefs(db, user_id)


@router.put("/users/{user_id}/scheduling-preferences", response_model=SchedulingPrefsOut)
async def put_user_prefs(user_id: uuid.UUID, body: SchedulingPrefsIn, _: InterviewConfigure, db: DB) -> Any:
    return await svc.settings_api.put_prefs(get_context(), db, user_id, body)


# ================================================================ settings
@router.get("/settings/agents/scheduling", response_model=SchedulingSettingsOut)
async def get_settings(_: Configure, db: DB) -> Any:
    return await svc.settings_api.get_settings(db)


@router.put("/settings/agents/scheduling", response_model=SchedulingSettingsOut)
async def put_settings(body: SchedulingSettingsIn, _: Configure, db: DB) -> Any:
    _human()
    return await svc.settings_api.update_settings(get_context(), db, body)
