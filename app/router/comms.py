"""Communication endpoints (Epic 10, story 10.1): email templates, sending, email log, bounce webhook.

* ``/email-templates`` (+ ``/merge-fields``, ``/preview``, ``/{id}/variants/{language}``, ``/{id}/archive``)
* ``POST /emails`` (202), ``GET /emails/address-status``, ``GET /emails/{id}``
* ``GET /{entity}/{id}/emails`` (generic, mounted before the activity router)
* ``POST /public/webhooks/email-bounce`` (no session; HMAC ``X-TOS-Signature``)

Each endpoint calls one service function.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Query, Request, status

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.comms import (
    AddressStatusOut,
    BounceOut,
    EmailDetailOut,
    EmailOut,
    EmailSendIn,
    EmailSendOut,
    MergeFieldGroupOut,
    PreviewIn,
    PreviewOut,
    TemplateCreate,
    TemplateDetailOut,
    TemplateKey,
    TemplateOut,
    TemplateUpdate,
    VariantIn,
)
from app.services import comms_service as svc

templates_router = APIRouter(prefix="/email-templates", tags=["email-templates"])
emails_router = APIRouter(prefix="/emails", tags=["emails"])
entity_router = APIRouter(tags=["emails"])
webhooks_router = APIRouter(prefix="/public/webhooks", tags=["webhooks"])

TemplateRead = Annotated[Principal, Depends(require("email_template:read"))]
TemplateManage = Annotated[Principal, Depends(require("email_template:manage"))]
Send = Annotated[Principal, Depends(require("email:send"))]
DB = TenantDBSessionDep
EntitySegment = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_-]{1,63}$")]


# ================================================================ templates
@templates_router.get("", response_model=list[TemplateOut])
async def list_templates(
    _: TemplateRead,
    db: DB,
    key: Annotated[TemplateKey | None, Query()] = None,
    include_archived: bool = False,
) -> Any:
    return await svc.list_templates(get_context(), db, key, include_archived)


@templates_router.post("", response_model=TemplateDetailOut, status_code=status.HTTP_201_CREATED)
async def create_template(body: TemplateCreate, _: TemplateManage, db: DB) -> Any:
    return await svc.create_template(get_context(), db, body)


@templates_router.get("/merge-fields", response_model=list[MergeFieldGroupOut])
async def merge_fields(_: TemplateRead, key: Annotated[TemplateKey | None, Query()] = None) -> Any:
    return svc.merge_field_catalog(key)


@templates_router.post("/preview", response_model=PreviewOut)
async def preview(body: PreviewIn, _: TemplateRead, db: DB) -> Any:
    return await svc.preview(get_context(), db, body)


@templates_router.get("/{template_id}", response_model=TemplateDetailOut)
async def get_template(template_id: uuid.UUID, _: TemplateRead, db: DB) -> Any:
    return await svc.get_template(get_context(), db, template_id)


@templates_router.patch("/{template_id}", response_model=TemplateDetailOut)
async def update_template(template_id: uuid.UUID, body: TemplateUpdate, _: TemplateManage, db: DB) -> Any:
    return await svc.update_template(get_context(), db, template_id, body)


@templates_router.put("/{template_id}/variants/{language}", response_model=TemplateDetailOut)
async def put_variant(
    template_id: uuid.UUID,
    language: Annotated[str, Path(min_length=2, max_length=5)],
    body: VariantIn,
    _: TemplateManage,
    db: DB,
) -> Any:
    return await svc.put_variant(get_context(), db, template_id, language, body)


@templates_router.post("/{template_id}/archive", response_model=TemplateDetailOut)
async def archive_template(template_id: uuid.UUID, _: TemplateManage, db: DB) -> Any:
    return await svc.archive_template(get_context(), db, template_id)


# ================================================================ emails
@emails_router.post("", response_model=EmailSendOut, status_code=status.HTTP_202_ACCEPTED)
async def send_email(body: EmailSendIn, _: Send, db: DB) -> Any:
    return await svc.send_emails(get_context(), db, body)


@emails_router.get("/address-status", response_model=list[AddressStatusOut])
async def address_status(
    _: Send,
    db: DB,
    addresses: Annotated[list[str], Query(max_length=50)] = [],  # ruff: ignore[mutable-argument-default]
) -> Any:
    flat = [a for item in addresses for a in item.split(",")]
    return await svc.address_status(get_context(), db, flat)


@emails_router.get("/{message_id}", response_model=EmailDetailOut)
async def get_email(message_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Any:
    return await svc.get_email(get_context(), db, message_id)


@entity_router.get("/{entity}/{entity_id}/emails", response_model=Page[EmailOut])
async def entity_emails(
    entity: EntitySegment,
    entity_id: uuid.UUID,
    _: CurrentUserDep,
    db: DB,
    params: Annotated[PageParams, Depends()],
) -> Any:
    return await svc.list_entity_emails(get_context(), db, entity, entity_id, params)


# ================================================================ bounce webhook
@webhooks_router.post("/email-bounce", response_model=BounceOut)
async def email_bounce(
    request: Request, x_tos_signature: Annotated[str | None, Header(alias="X-TOS-Signature")] = None
) -> Any:
    return await svc.handle_bounce_webhook(await request.body(), x_tos_signature)
