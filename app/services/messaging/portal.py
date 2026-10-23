# ruff: file-ignore[line-too-long]
"""Recipient self-service for SMS / WhatsApp (Epic 33, FR-172/173).

* Candidate portal "Messages" (``/api/v1/portal/messages``): the signed-in candidate / Worker toggles their own
  WhatsApp consent (purpose ``whatsapp``, against the current notice) and SMS opt-out -- never anyone else's
  (the session is org-scoped to the worker).
* Public opt-out link (secure link purpose ``channel_opt_out``, 180 days, 5 uses): GET shows the channel, POST
  opts the number out (and withdraws WhatsApp consent).
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound
from app.models.comms import ChannelMessage
from app.schemas.messaging import PortalMessagesOut, PortalSmsIn, PortalWhatsAppIn, StatusOut
from app.services.messaging import opt_outs, sending
from app.services.messaging.notices import ensure_whatsapp_notice

OPT_OUT = sending.OPT_OUT_PURPOSE


async def get_messages(db: AsyncSession, candidate_id: uuid.UUID) -> PortalMessagesOut:
    phone = await sending.resolve_phone(db, "candidate", candidate_id)
    return PortalMessagesOut(
        has_phone=bool(phone),
        sms_opted_out=await opt_outs.is_opted_out(db, "sms", phone),
        whatsapp_opted_in=await sending.has_whatsapp_consent(db, "candidate", candidate_id),
        whatsapp_notice=await sending.whatsapp_notice(db),
    )


async def set_whatsapp(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, data: PortalWhatsAppIn
) -> PortalMessagesOut:
    from app.services.privacy import consent

    opted_in = await sending.has_whatsapp_consent(db, "candidate", candidate_id)
    if data.enabled and not opted_in:
        notice = await ensure_whatsapp_notice(db)
        if notice is None or notice.id != data.notice_id:
            raise ConflictError("The WhatsApp notice changed; please review it again", code="notice_outdated")
        await consent.grant_consent(
            ctx, db, candidate_id, sending.WHATSAPP_PURPOSE, source="candidate_portal", notice_id=notice.id,
            evidence={"channel": "candidate_portal"},
        )  # fmt: skip
        phone = await sending.resolve_phone(db, "candidate", candidate_id)
        if phone:
            await opt_outs.revoke_for(db, "whatsapp", phone, "opted in from the candidate portal")
    elif not data.enabled and opted_in:
        await consent.withdraw_consent(ctx, db, candidate_id, sending.WHATSAPP_PURPOSE, by_kind="candidate")
    return await get_messages(db, candidate_id)


async def set_sms(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, data: PortalSmsIn
) -> PortalMessagesOut:
    phone = await sending.resolve_phone(db, "candidate", candidate_id)
    if not phone:
        raise ConflictError("Add a phone number to your profile first", code="no_phone")
    if data.opted_out:
        await opt_outs.record(
            db, "sms", phone, "candidate_portal", recipient_kind="candidate", recipient_id=candidate_id
        )
    else:
        await opt_outs.revoke_for(db, "sms", phone, "opted back in from the candidate portal")
    return await get_messages(db, candidate_id)


# ================================================================ public opt-out link
async def link_view(token: str) -> tuple[secure_links.ResolvedLink, str, bool]:
    """``(link, channel, already opted out)`` -- resolving never consumes a use."""
    link = await secure_links.resolve(token, (OPT_OUT,))
    async with tenant_session(link.tenant_id) as db:
        message = await db.get(ChannelMessage, link.subject_id)
        if message is None:
            raise NotFound("Link not found", code="link_invalid")
        return link, message.channel, await opt_outs.is_opted_out(db, message.channel, message.to_e164)


async def link_opt_out(token: str) -> StatusOut:
    link = await secure_links.resolve(token, (OPT_OUT,))
    async with tenant_session(link.tenant_id) as db:
        message = await db.get(ChannelMessage, link.subject_id)
        if message is None or not message.to_e164:
            raise NotFound("Link not found", code="link_invalid")
        await secure_links.consume(db, link.id)
        await opt_outs.record(
            db,
            message.channel,
            message.to_e164,
            "recipient_link",
            recipient_kind=message.recipient_kind,
            recipient_id=message.recipient_id,
        )
        if message.channel == "whatsapp" and message.recipient_kind == "candidate" and message.recipient_id:
            from app.services.privacy import consent

            sctx = RequestContext(tenant_id=link.tenant_id, actor_kind="system")
            await consent.withdraw_consent(
                sctx, db, message.recipient_id, sending.WHATSAPP_PURPOSE, by_kind="candidate"
            )
    return StatusOut(status="opted_out")
