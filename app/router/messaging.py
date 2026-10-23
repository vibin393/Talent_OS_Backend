# ruff: file-ignore[line-too-long]
"""Phase 2 Epic 33 endpoints (communication extensions). Internal users only -- external cookies get 401.

* ``/comms/*`` -- SMS / WhatsApp: channel settings, templates, eligibility + send (``Idempotency-Key``),
  message history, messaging state + WhatsApp attestation, opt-outs, manual queue, unmatched inbound, connectors.
* ``/connections/{provider}/enable-reply-capture``, ``/connections/me/reply-capture[/disable]`` (FR-171).
* ``GET /emails/{id}/thread`` -- the email and its captured replies (P1 email read rules).
* ``/career-site/custom-domain`` (``career_domain:manage``) and the public ``GET /public/careers/resolve-host``.
* ``/portal/messages`` -- the signed-in candidate's own WhatsApp opt-in and SMS opt-out.

Each endpoint calls one service function.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.core.context import get_context
from app.core.errors import NotFound
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.dependencies.candidate import CandidateDBDep, CandidateDep
from app.schemas.connections import StartOut
from app.schemas.connectors import ConnectorConfigOut
from app.schemas.messaging import (
    AttestationIn,
    Channel,
    ChannelMessageOut,
    ChannelSettingsOut,
    ChannelSettingsUpdate,
    ChannelTemplateIn,
    ChannelTemplateOut,
    ChannelTemplateUpdate,
    CustomDomainIn,
    CustomDomainOut,
    EligibilityIn,
    EligibilityOut,
    EmailThreadOut,
    ManualQueueItemOut,
    MarkFailedIn,
    MessagingConnectorIn,
    MessagingConnectorUpdate,
    MessagingStateOut,
    OptOutIn,
    OptOutOut,
    PortalMessagesOut,
    PortalSmsIn,
    PortalWhatsAppIn,
    ReplyCaptureStatusOut,
    ResolveHostOut,
    RevokeIn,
    SendMessagesIn,
    SendMessagesOut,
    SendTestIn,
    StatusOut,
    TemplateSyncOut,
)
from app.services import connections_service, custom_domains
from app.services.comms import channel_settings, inbound
from app.services.messaging import connectors_admin, opt_outs, portal, sending, templates

comms_router = APIRouter(prefix="/comms", tags=["messaging"])
reply_router = APIRouter(tags=["reply-capture"])
domain_router = APIRouter(prefix="/career-site/custom-domain", tags=["career-site"])
public_router = APIRouter(prefix="/public/careers", tags=["careers"])
portal_router = APIRouter(prefix="/portal/messages", tags=["portal"])

Configure = Annotated[Principal, Depends(require("messaging:configure"))]
Send = Annotated[Principal, Depends(require("messaging:send"))]
SendOrConfigure = Annotated[Principal, Depends(require_any("messaging:send", "messaging:configure"))]
ManageOwn = Annotated[Principal, Depends(require("connection:manage_own"))]
DomainManage = Annotated[Principal, Depends(require("career_domain:manage"))]
DB = TenantDBSessionDep


# ================================================================ channel settings / templates
@comms_router.get("/channel-settings", response_model=ChannelSettingsOut)
async def get_channel_settings(_: Configure, db: DB) -> Any:
    return await channel_settings.get_settings(get_context(), db)


@comms_router.patch("/channel-settings", response_model=ChannelSettingsOut)
async def update_channel_settings(body: ChannelSettingsUpdate, _: Configure, db: DB) -> Any:
    return await channel_settings.update_settings(get_context(), db, body)


@comms_router.get("/channel-templates", response_model=list[ChannelTemplateOut])
async def list_channel_templates(
    _: SendOrConfigure,
    db: DB,
    channel: Channel | None = None,
    template_status: Annotated[str | None, Query(alias="status")] = None,
) -> Any:
    return await templates.list_templates(get_context(), db, channel, template_status)


@comms_router.post(
    "/channel-templates", response_model=ChannelTemplateOut, status_code=status.HTTP_201_CREATED
)
async def create_channel_template(body: ChannelTemplateIn, _: Configure, db: DB) -> Any:
    return await templates.create_template(get_context(), db, body)


@comms_router.post("/channel-templates/sync-whatsapp", response_model=TemplateSyncOut)
async def sync_whatsapp_templates(_: Configure, db: DB) -> Any:
    return await templates.sync_whatsapp(get_context(), db)


@comms_router.patch("/channel-templates/{template_id}", response_model=ChannelTemplateOut)
async def update_channel_template(
    template_id: uuid.UUID, body: ChannelTemplateUpdate, _: Configure, db: DB
) -> Any:
    return await templates.update_template(get_context(), db, template_id, body)


# ================================================================ sending
@comms_router.post("/messages/eligibility", response_model=list[EligibilityOut])
async def eligibility(body: EligibilityIn, _: Send, db: DB) -> Any:
    return await sending.eligibility(
        get_context(), db, body.channel, body.template_id, body.recipients, body.context_refs
    )


@comms_router.post("/messages/send", response_model=SendMessagesOut)
async def send_messages(
    body: SendMessagesIn,
    _: Send,
    db: DB,
    idem: Annotated[IdempotencyGuard, Depends(idempotency(required=False))],
) -> Any:
    return await idem.run(
        lambda: sending.send(
            get_context(), db, body.channel, body.template_id, body.recipients, body.context_refs
        )
    )


@comms_router.get("/messages", response_model=list[ChannelMessageOut])
async def list_messages(
    _: SendOrConfigure,
    db: DB,
    entity_type: Annotated[str | None, Query(pattern=r"^(candidate|contact)$")] = None,
    entity_id: uuid.UUID | None = None,
    channel: Channel | None = None,
) -> Any:
    return await sending.list_messages(get_context(), db, entity_type, entity_id, channel)


@comms_router.get("/messaging-state/{entity_type}/{entity_id}", response_model=MessagingStateOut)
async def messaging_state(entity_type: str, entity_id: uuid.UUID, _: Send, db: DB) -> Any:
    return await sending.messaging_state(get_context(), db, entity_type, entity_id)


@comms_router.post("/whatsapp-attestation", response_model=MessagingStateOut)
async def whatsapp_attestation(body: AttestationIn, _: Send, db: DB) -> Any:
    return await sending.attest_whatsapp(get_context(), db, body.candidate_id, body.notice_id)


# ================================================================ opt-outs / manual queue / unmatched inbound
@comms_router.get("/opt-outs", response_model=list[OptOutOut])
async def list_opt_outs(
    _: Configure,
    db: DB,
    channel: Channel | None = None,
    q: Annotated[str | None, Query(max_length=30)] = None,
) -> Any:
    return await opt_outs.list_opt_outs(get_context(), db, channel, q)


@comms_router.post("/opt-outs", response_model=OptOutOut, status_code=status.HTTP_201_CREATED)
async def add_opt_out(body: OptOutIn, _: Configure, db: DB) -> Any:
    return await opt_outs.add_opt_out(get_context(), db, body)


@comms_router.post("/opt-outs/{opt_out_id}/revoke", response_model=OptOutOut)
async def revoke_opt_out(opt_out_id: uuid.UUID, body: RevokeIn, _: Configure, db: DB) -> Any:
    return await opt_outs.revoke_opt_out(get_context(), db, opt_out_id, body.reason)


@comms_router.get("/manual-queue", response_model=list[ManualQueueItemOut])
async def manual_queue(_: Configure, db: DB) -> Any:
    return await sending.manual_queue(get_context(), db)


@comms_router.post("/manual-queue/{message_id}/mark-sent", response_model=ChannelMessageOut)
async def mark_sent(message_id: uuid.UUID, _: Configure, db: DB) -> Any:
    return await sending.mark_sent(get_context(), db, message_id)


@comms_router.post("/manual-queue/{message_id}/mark-failed", response_model=ChannelMessageOut)
async def mark_failed(message_id: uuid.UUID, body: MarkFailedIn, _: Configure, db: DB) -> Any:
    return await sending.mark_failed(get_context(), db, message_id, body.error_code)


@comms_router.get("/inbound/unmatched", response_model=list[ChannelMessageOut])
async def unmatched_inbound(_: Configure, db: DB) -> Any:
    return await sending.unmatched_inbound(get_context(), db)


# ================================================================ connectors (sms / whatsapp)
@comms_router.get("/connectors", response_model=connectors_admin.MessagingConnectorsOut)
async def list_connectors(_: Configure, db: DB, category: Channel) -> Any:
    return await connectors_admin.list_connectors(get_context(), db, category)


@comms_router.post("/connectors", response_model=ConnectorConfigOut, status_code=status.HTTP_201_CREATED)
async def create_connector(body: MessagingConnectorIn, _: Configure, db: DB) -> Any:
    return await connectors_admin.create_connector(get_context(), db, body)


@comms_router.patch("/connectors/{config_id}", response_model=ConnectorConfigOut)
async def update_connector(config_id: uuid.UUID, body: MessagingConnectorUpdate, _: Configure, db: DB) -> Any:
    return await connectors_admin.update_connector(get_context(), db, config_id, body)


@comms_router.post("/connectors/{config_id}/send-test", response_model=StatusOut)
async def send_test(config_id: uuid.UUID, body: SendTestIn, _: Configure, db: DB) -> Any:
    return await connectors_admin.send_test(get_context(), db, config_id, body.to_e164)


# ================================================================ reply capture (FR-171)
@reply_router.post("/connections/{provider}/enable-reply-capture", response_model=StartOut)
async def enable_reply_capture(provider: str, _: ManageOwn) -> Any:
    return await connections_service.start(get_context(), provider, reply_capture=True)


@reply_router.get("/connections/me/reply-capture", response_model=ReplyCaptureStatusOut)
async def reply_capture_status(_: ManageOwn, db: DB) -> Any:
    return await inbound.reply_capture_status(get_context(), db)


@reply_router.post("/connections/me/reply-capture/disable", response_model=ReplyCaptureStatusOut)
async def disable_reply_capture(_: ManageOwn, db: DB) -> Any:
    return await inbound.disable_reply_capture(get_context(), db)


@reply_router.get("/emails/{message_id}/thread", response_model=EmailThreadOut)
async def email_thread(message_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Any:
    return await inbound.get_thread(get_context(), db, message_id)


# ================================================================ custom career domain (FR-174)
@domain_router.get("", response_model=CustomDomainOut | None)
async def get_custom_domain(_: DomainManage) -> Any:
    return await custom_domains.get_custom_domain(get_context())


@domain_router.post("", response_model=CustomDomainOut, status_code=status.HTTP_201_CREATED)
async def add_custom_domain(body: CustomDomainIn, _: DomainManage, db: DB) -> Any:
    return await custom_domains.add_custom_domain(get_context(), db, body.host)


@domain_router.delete("", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def remove_custom_domain(_: DomainManage, db: DB) -> Response:
    await custom_domains.remove_custom_domain(get_context(), db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@domain_router.post("/verify-now", response_model=CustomDomainOut)
async def verify_custom_domain(_: DomainManage) -> Any:
    return await custom_domains.verify_custom_domain(get_context())


@public_router.get("/resolve-host", response_model=ResolveHostOut)
async def resolve_host(
    response: Response, request: Request, host: Annotated[str | None, Query(max_length=253)] = None
) -> Any:
    """Host-based career site resolution for the Next.js proxy (cached 5 minutes at the edge)."""
    slug = await custom_domains.slug_for_host(
        host or request.headers.get("x-forwarded-host") or request.headers.get("host")
    )
    if slug is None:
        raise NotFound("Career site not found", code="career_site_not_found")
    response.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
    return ResolveHostOut(slug=slug)


# ================================================================ candidate portal: Messages
def _pctx(principal: Any) -> Any:
    return principal.ctx(get_context())


@portal_router.get("", response_model=PortalMessagesOut)
async def portal_messages(principal: CandidateDep, db: CandidateDBDep, response: Response) -> Any:
    response.headers["Cache-Control"] = "private, no-store"
    return await portal.get_messages(db, principal.candidate_id)


@portal_router.put("/whatsapp", response_model=PortalMessagesOut)
async def portal_whatsapp(body: PortalWhatsAppIn, principal: CandidateDep, db: CandidateDBDep) -> Any:
    return await portal.set_whatsapp(_pctx(principal), db, principal.candidate_id, body)


@portal_router.put("/sms", response_model=PortalMessagesOut)
async def portal_sms(body: PortalSmsIn, principal: CandidateDep, db: CandidateDBDep) -> Any:
    return await portal.set_sms(_pctx(principal), db, principal.candidate_id, body)
