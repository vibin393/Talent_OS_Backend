# ruff: file-ignore[line-too-long]
"""Demand intake endpoints (Epic 55, A6). Mounted **before** the P1 demands router (``/demands/{id}`` would capture
``/demands/intake-messages``).

* ``POST /inbound/demand-intake`` -- public; HMAC ``X-Intake-Signature``, recipient ``X-Intake-Recipient`` (202).
* ``/demands/intake-messages`` -- the inbox (``demand:create``); mutations are human-only (API keys read only).
* ``/demands/intake-settings`` (+ ``/rotate-address``) and ``/demands/intake-sender-domains`` -- ``settings:manage``.
* ``/demands/agent-settings`` -- ``ai:agent_configure`` (GET also ``ai:govern``).

Each endpoint calls one service function.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile, status

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.demand_intake import (
    AgentSettingsIn,
    AgentSettingsOut,
    AssignClientIn,
    DiscardIn,
    IntakeMessageOut,
    IntakeMessageSummaryOut,
    IntakeQuery,
    IntakeReceiptOut,
    IntakeSettingsIn,
    IntakeSettingsOut,
    IntakeStatus,
    SenderDomainIn,
    SenderDomainOut,
)
from app.services import demand_intake_service as svc

router = APIRouter(prefix="/demands", tags=["demand-intake"])
inbound_router = APIRouter(prefix="/inbound", tags=["demand-intake"])

Create = Annotated[Principal, Depends(require("demand:create"))]
SettingsManage = Annotated[Principal, Depends(require("settings:manage"))]
AgentConfigure = Annotated[Principal, Depends(require("ai:agent_configure"))]
AgentSettingsRead = Annotated[Principal, Depends(require_any("ai:agent_configure", "ai:govern"))]
Paging = Annotated[PageParams, Depends()]
DB = TenantDBSessionDep


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


# ================================================================ inbound webhook (public)
@inbound_router.post("/demand-intake", response_model=IntakeReceiptOut, status_code=status.HTTP_202_ACCEPTED)
async def inbound_demand_intake(
    request: Request,
    x_intake_signature: Annotated[str | None, Header(alias="X-Intake-Signature")] = None,
    x_intake_recipient: Annotated[str | None, Header(alias="X-Intake-Recipient")] = None,
) -> Any:
    raw = await request.body()
    return await svc.receive_platform_email(raw, x_intake_recipient, x_intake_signature)


# ================================================================ inbox
@router.get("/intake-messages", response_model=Page[IntakeMessageSummaryOut])
async def list_intake_messages(
    _: Create,
    db: DB,
    params: Paging,
    tab: Annotated[str | None, Query(pattern="^(needs_attention|drafted|all)$")] = None,
    status_: Annotated[list[IntakeStatus] | None, Query(alias="status")] = None,
    client_id: uuid.UUID | None = None,
) -> Any:
    q = IntakeQuery(status=status_, tab=tab, client_id=client_id)  # type: ignore[arg-type]
    return await svc.list_messages(get_context(), db, q, params)


@router.post("/intake-messages", response_model=IntakeMessageOut, status_code=status.HTTP_201_CREATED)
async def create_intake_message(
    _: Create,
    db: DB,
    client_id: Annotated[uuid.UUID, Form()],
    text: Annotated[str | None, Form(max_length=200_000)] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> Any:
    _human()
    return await svc.create_from_upload(get_context(), db, client_id=client_id, text=text, upload=file)


@router.get("/intake-messages/{message_id}", response_model=IntakeMessageOut)
async def get_intake_message(message_id: uuid.UUID, _: Create, db: DB) -> Any:
    return await svc.get_message(get_context(), db, message_id)


@router.post("/intake-messages/{message_id}/assign", response_model=IntakeMessageOut)
async def assign_intake_client(
    message_id: uuid.UUID,
    body: AssignClientIn,
    _: Create,
    __: Annotated[Principal, Depends(require("client:read"))],
    db: DB,
) -> Any:
    return await svc.assign_client(get_context(), db, message_id, body)


@router.post("/intake-messages/{message_id}/retry", response_model=IntakeMessageOut)
async def retry_intake_message(message_id: uuid.UUID, _: Create, db: DB) -> Any:
    return await svc.retry(get_context(), db, message_id)


@router.post("/intake-messages/{message_id}/discard", response_model=IntakeMessageOut)
async def discard_intake_message(message_id: uuid.UUID, body: DiscardIn, _: Create, db: DB) -> Any:
    return await svc.discard(get_context(), db, message_id, body)


# ================================================================ intake settings / sender domains
@router.get("/intake-settings", response_model=IntakeSettingsOut)
async def get_intake_settings(_: SettingsManage, db: DB) -> Any:
    return await svc.get_intake_settings(get_context(), db)


@router.put("/intake-settings", response_model=IntakeSettingsOut)
async def put_intake_settings(body: IntakeSettingsIn, _: SettingsManage, db: DB) -> Any:
    _human()
    return await svc.update_intake_settings(get_context(), db, body)


@router.post("/intake-settings/rotate-address", response_model=IntakeSettingsOut)
async def rotate_intake_address(_: SettingsManage, db: DB) -> Any:
    _human()
    return await svc.rotate_address(get_context(), db)


@router.get("/intake-sender-domains", response_model=list[SenderDomainOut])
async def list_sender_domains(_: SettingsManage, db: DB) -> Any:
    return await svc.list_sender_domains(get_context(), db)


@router.post("/intake-sender-domains", response_model=SenderDomainOut, status_code=status.HTTP_201_CREATED)
async def add_sender_domain(body: SenderDomainIn, _: SettingsManage, db: DB) -> Any:
    _human()
    return await svc.add_sender_domain(get_context(), db, body)


@router.delete("/intake-sender-domains/{domain_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_sender_domain(domain_id: uuid.UUID, _: SettingsManage, db: DB) -> Response:
    _human()
    await svc.remove_sender_domain(get_context(), db, domain_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ================================================================ agent settings
@router.get("/agent-settings", response_model=AgentSettingsOut)
async def get_agent_settings(_: AgentSettingsRead, db: DB) -> Any:
    return await svc.get_agent_settings(get_context(), db)


@router.put("/agent-settings", response_model=AgentSettingsOut)
async def put_agent_settings(body: AgentSettingsIn, _: AgentConfigure, db: DB) -> Any:
    _human()
    return await svc.update_agent_settings(get_context(), db, body)
