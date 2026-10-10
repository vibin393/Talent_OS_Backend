# ruff: noqa: E501, D401
"""Telephony API (Epic 76, spec A8).

| Method + path | Permission |
|---|---|
| ``GET /telephony/call-options?application_id|candidate_id|voice_session_id`` | ``voice:call`` |
| ``POST /telephony/calls`` + Idempotency-Key (★ human only) | ``voice:call`` |
| ``GET /telephony/calls`` · ``GET /telephony/calls/{id}`` | ``voice:call`` or ``voice:upload`` |
| ``POST /telephony/calls/{id}/hangup`` · ``POST /telephony/calls/{id}/do-not-call`` | ``voice:call`` |
| ``GET|PUT|DELETE /me/call-phone`` · ``POST /me/call-phone/verify`` | any internal user |
| ``POST /users/{id}/call-phone/attest`` (★ human only) | ``integration:manage`` |
| ``GET|PUT /settings/telephony`` | ``integration:view`` / ``integration:manage`` |
| ``GET|POST /settings/telephony/numbers`` · ``PATCH|DELETE /settings/telephony/numbers/{id}`` | ``integration:manage`` |
| ``GET /telephony/usage`` | ``usage:read`` or ``integration:view`` |
| ``GET|POST /hooks/voice/{config_id}/answer/{call_token}`` | provider-signed (no session) |
| ``POST /hooks/voice/{config_id}/exotel/{path_token}`` | Exotel relay: path token + IP allow-list (no session) |

Responses never carry phone numbers (beyond ``last4`` and the Tenant's caller ID), provider refs or tokens.
"""

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.errors import Forbidden
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.pagination import Page
from app.core.permissions import Principal, current_principal, require, require_any
from app.core.settings import get_settings as app_settings
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.telephony import (
    CallOptionsOut,
    CallPhoneAttestIn,
    CallPhoneIn,
    CallPhoneOut,
    CallPhoneVerifyIn,
    DoNotCallOut,
    PlaceCallIn,
    TelephonyNumberIn,
    TelephonyNumberOut,
    TelephonyNumberUpdate,
    TelephonySettingsIn,
    TelephonySettingsOut,
    UsageOut,
    VoiceCallOut,
)
from app.services import telephony_service as svc

router = APIRouter(tags=["telephony"])
hooks_router = APIRouter(prefix="/hooks/voice", tags=["hooks"])
DB = TenantDBSessionDep
Call = Annotated[Principal, Depends(require("voice:call"))]
ReadCalls = Annotated[Principal, Depends(require_any("voice:call", "voice:upload"))]
Manage = Annotated[Principal, Depends(require("integration:manage"))]
View = Annotated[Principal, Depends(require_any("integration:view", "integration:manage"))]
Usage = Annotated[Principal, Depends(require_any("usage:read", "integration:view"))]
Signed = Annotated[Principal, Depends(current_principal)]


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


def _user(principal: Principal) -> None:
    if principal.type != "user":
        raise Forbidden("Only a signed-in user can do this", code="human_only_action")


# ------------------------------------------------------------------------------------------------ calls
@router.get("/telephony/call-options", response_model=CallOptionsOut)
async def call_options(
    _: Call,
    db: DB,
    application_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    voice_session_id: uuid.UUID | None = None,
) -> Any:
    return await svc.call_options(
        get_context(), db, application_id=application_id, candidate_id=candidate_id, voice_session_id=voice_session_id
    )


@router.post("/telephony/calls", response_model=VoiceCallOut, status_code=status.HTTP_201_CREATED)
async def place_call(
    body: PlaceCallIn, principal: Call, db: DB, idem: Annotated[IdempotencyGuard, Depends(idempotency())]
) -> Any:
    _user(principal)
    _human()
    ctx = get_context()

    async def _run() -> VoiceCallOut:
        return await svc.place_call(ctx, db, body)

    return await idem.run(_run)


@router.get("/telephony/calls", response_model=Page[VoiceCallOut])
async def list_calls(
    _: ReadCalls,
    db: DB,
    voice_session_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    placed_by: uuid.UUID | None = None,
    call_status: Annotated[str | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Any:
    return await svc.list_calls(
        get_context(), db, voice_session_id=voice_session_id, candidate_id=candidate_id, placed_by=placed_by,
        status=call_status, page=page, page_size=page_size,
    )  # fmt: skip


@router.get("/telephony/calls/{call_id}", response_model=VoiceCallOut)
async def get_call(call_id: uuid.UUID, _: ReadCalls, db: DB) -> Any:
    return await svc.get_call(get_context(), db, call_id)


@router.post("/telephony/calls/{call_id}/hangup", response_model=VoiceCallOut)
async def hangup(call_id: uuid.UUID, principal: Call, db: DB) -> Any:
    _user(principal)
    return await svc.hangup(get_context(), db, call_id)


@router.post("/telephony/calls/{call_id}/do-not-call", response_model=DoNotCallOut)
async def do_not_call(call_id: uuid.UUID, principal: Call, db: DB) -> Any:
    _user(principal)
    _human()
    return await svc.do_not_call(get_context(), db, call_id)


@router.get("/telephony/usage", response_model=UsageOut)
async def usage(
    _: Usage,
    db: DB,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    group_by: Literal["user", "legal_entity", "day"] = "day",
) -> Any:
    return await svc.usage(get_context(), db, date_from=date_from, date_to=date_to, group_by=group_by)


# ------------------------------------------------------------------------------------------------ phones
@router.get("/me/call-phone", response_model=CallPhoneOut)
async def my_phone(principal: Signed, db: DB) -> Any:
    _user(principal)
    return await svc.my_phone(get_context(), db)


@router.put("/me/call-phone", response_model=CallPhoneOut)
async def start_phone(body: CallPhoneIn, principal: Signed, db: DB) -> Any:
    _user(principal)
    _human()
    return await svc.start_phone(get_context(), db, body.phone_e164)


@router.post("/me/call-phone/verify", response_model=CallPhoneOut)
async def verify_phone(body: CallPhoneVerifyIn, principal: Signed, db: DB) -> Any:
    _user(principal)
    _human()
    return await svc.verify_phone(get_context(), db, body.code)


@router.delete("/me/call-phone", status_code=status.HTTP_204_NO_CONTENT)
async def remove_phone(principal: Signed, db: DB) -> Response:
    _user(principal)
    await svc.remove_phone(get_context(), db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/users/{user_id}/call-phone/attest", response_model=CallPhoneOut)
async def attest_phone(user_id: uuid.UUID, body: CallPhoneAttestIn, principal: Manage, db: DB) -> Any:
    _user(principal)
    _human()
    return await svc.attest_phone(get_context(), db, user_id, body.phone_e164, body.reason)


# ------------------------------------------------------------------------------------------------ settings
@router.get("/settings/telephony", response_model=TelephonySettingsOut)
async def get_settings(_: View, db: DB) -> Any:
    return await svc.get_settings(get_context(), db)


@router.put("/settings/telephony", response_model=TelephonySettingsOut)
async def update_settings(body: TelephonySettingsIn, principal: Manage, db: DB) -> Any:
    _user(principal)
    _human()
    return await svc.update_settings(get_context(), db, body)


@router.get("/settings/telephony/numbers", response_model=list[TelephonyNumberOut])
async def list_numbers(_: View, db: DB) -> Any:
    return await svc.list_numbers(get_context(), db)


@router.post("/settings/telephony/numbers", response_model=TelephonyNumberOut, status_code=status.HTTP_201_CREATED)
async def create_number(body: TelephonyNumberIn, principal: Manage, db: DB) -> Any:
    _user(principal)
    _human()
    return await svc.create_number(get_context(), db, body)


@router.patch("/settings/telephony/numbers/{number_id}", response_model=TelephonyNumberOut)
async def update_number(number_id: uuid.UUID, body: TelephonyNumberUpdate, principal: Manage, db: DB) -> Any:
    _user(principal)
    _human()
    return await svc.update_number(get_context(), db, number_id, body)


@router.delete("/settings/telephony/numbers/{number_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_number(number_id: uuid.UUID, principal: Manage, db: DB) -> Response:
    _user(principal)
    _human()
    await svc.deactivate_number(get_context(), db, number_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------------------------------------ provider endpoints
def _public_url(request: Request) -> str:
    """The URL the provider signed: rebuilt from ``public_webhook_base_url`` (never the Host header)."""
    url = f"{app_settings().p5_public_webhook_base_url()}{request.url.path}"
    return f"{url}?{request.url.query}" if request.url.query else url


@hooks_router.api_route("/{config_id}/answer/{call_token}", methods=["GET", "POST"], response_class=Response)
async def answer(config_id: uuid.UUID, call_token: str, request: Request) -> Response:
    body = await request.body()
    media_type, doc = await svc.answer(config_id, call_token, _public_url(request), dict(request.headers), body)
    return Response(content=doc, media_type=media_type)


@hooks_router.post("/{config_id}/exotel/{path_token}", status_code=status.HTTP_202_ACCEPTED, response_model=dict[str, str])
async def exotel_relay(config_id: uuid.UUID, path_token: str, request: Request, response: Response) -> Any:
    body = await request.body()
    ack = await svc.relay_exotel(
        config_id, path_token, dict(request.headers), body, request.client.host if request.client else None
    )
    if ack.status == "duplicate":
        response.status_code = status.HTTP_200_OK
    return {"status": ack.status, "event_id": str(ack.event_id)}
