# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Channel delivery of conversation turns and invitations (Epic 57 A3, AD-70 one send path).

* ``portal_chat`` / ``link_chat``: the stored turn IS the delivery (the Candidate reads it in the portal / on the
  Secure-Link page); the invitation adds an in-app portal notification or the Secure-Link email.
* ``email``: every outbound turn is an email through ``comms_service.queue_email`` (``origin`` agent / user, reply-to =
  the Accountable Owner so P2 reply capture threads the answer).
* ``whatsapp``: the invitation is the tenant's approved WhatsApp template through ``messaging.sending.send`` (opt-in,
  opt-outs, DLT and the session rules apply there). P2 has no non-template ("session") WhatsApp send, so in-window
  turns are stored and readable through the conversation's chat link; replies on WhatsApp are processed as usual
  (recorded deviation).
"""

import uuid
from datetime import timedelta
from html import escape
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.context import RequestContext
from app.core.settings import get_settings
from app.models.screening_agent import ScreeningConversation, ScreeningTurn
from app.services import entity_refs
from app.services.screening_agent.common import LINK_PURPOSE, LINK_SUBJECT, now, settings_row

LINK_MAX_USES = 100_000  # a multi-use chat link (viewing never consumes; posting counts)
INVITE_TEMPLATE_KEY = "ai_screening_invite"


async def owner_name(db: AsyncSession, conv: ScreeningConversation) -> str:
    info = await entity_refs.user(db, conv.owner_user_id)
    return info.name if info is not None and info.name else "your recruiter"


async def company_name(db: AsyncSession, conv: ScreeningConversation) -> str:
    info = await entity_refs.tenant(db, conv.tenant_id)
    return info.name or "our team"


async def demand_title(db: AsyncSession, conv: ScreeningConversation) -> str:
    from app.models.demands import Demand

    row = await db.get(Demand, conv.demand_id)
    return row.title if row is not None else "the role"


def disclosure_line(name: str) -> str:
    return get_settings().agent_disclosure_line.format(owner_name=name)


async def issue_chat_link(db: AsyncSession, conv: ScreeningConversation) -> str:
    """A fresh Secure Link to the conversation chat (previous ones revoked); expiry = the conversation expiry."""
    await secure_links.revoke_for_subject(db, LINK_SUBJECT, conv.id, LINK_PURPOSE)
    ttl = max(conv.expires_at - now(), timedelta(hours=1))
    link, token = await secure_links.issue(
        db, tenant_id=conv.tenant_id, purpose=LINK_PURPOSE, subject_type=LINK_SUBJECT, subject_id=conv.id,
        ttl=ttl, max_uses=LINK_MAX_USES, created_by=conv.owner_user_id, metadata={"candidate_id": str(conv.candidate_id)},
    )  # fmt: skip
    conv.secure_link_id = link.id
    await db.flush()
    return secure_links.link_url(token)


def portal_url(conv: ScreeningConversation) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/portal/screening/{conv.id}"


async def _email(
    ctx: RequestContext,
    db: AsyncSession,
    conv: ScreeningConversation,
    subject: str,
    html: str,
    *,
    origin: str,
    sender_user_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    from app.services import comms_service

    try:
        recipient = await comms_service.recipient_for(db, "candidate", conv.candidate_id)
    except Exception:
        return None
    result = await comms_service.queue_email(
        ctx, db, origin=origin, to=[recipient], subject=subject, body_html=html,
        related=[("candidate", conv.candidate_id), ("application", conv.application_id)],
        refs={"candidate_id": conv.candidate_id, "demand_id": conv.demand_id},
        sender_user_id=sender_user_id, reply_to_user_id=conv.owner_user_id, raw=True,
    )  # fmt: skip
    return result.message_ids[0] if result.message_ids else None


def _html(body: str) -> str:
    return "".join(f"<p>{escape(line)}</p>" for line in body.splitlines() if line.strip())


async def deliver(
    ctx: RequestContext,
    db: AsyncSession,
    conv: ScreeningConversation,
    turn: ScreeningTurn,
    *,
    origin: str = "agent",
    sender_user_id: uuid.UUID | None = None,
) -> bool:
    """Deliver a stored outbound turn on the conversation channel. Returns whether a message left the platform."""
    if conv.channel != "email":
        return False
    name = await owner_name(db, conv)
    title = await demand_title(db, conv)
    footer = "" if origin == "user" else f"<p><em>{escape(disclosure_line(name))}</em></p>"
    message_id = await _email(
        ctx, db, conv, f"Re: {title} - screening questions", _html(turn.body) + footer,
        origin=origin, sender_user_id=sender_user_id,
    )  # fmt: skip
    turn.email_message_id = message_id
    await db.flush()
    return message_id is not None


async def _render_invite(db: AsyncSession, conv: ScreeningConversation, chat_link: str) -> tuple[str, str]:
    from app.models.comms import EmailTemplateVariant
    from app.services import comms_service
    from app.services.comms import merge_fields

    template = await comms_service.active_template(db, INVITE_TEMPLATE_KEY)
    variant = (
        (
            await db.execute(select(EmailTemplateVariant).where(EmailTemplateVariant.template_id == template.id))
        ).scalars().first()
        if template is not None
        else None
    )  # fmt: skip
    recipient = await entity_refs.addressee(db, "candidate", conv.candidate_id)
    data: dict[str, Any] = await entity_refs.merge_data(
        db, conv.tenant_id, {"candidate_id": conv.candidate_id, "demand_id": conv.demand_id, "link_url": chat_link},
        sender_user_id=conv.owner_user_id, recipient=recipient,
    )  # fmt: skip
    data["screening"] = {"chat_link": chat_link}
    if variant is None:
        title = await demand_title(db, conv)
        return (
            f"A few questions about the {title} role",
            f'<p>Please answer a few short questions about the {escape(title)} role.</p><p><a href="{escape(chat_link)}">Open the chat</a></p>',
        )
    subject, _ = merge_fields.render(variant.subject, data, escape=False)
    body, _ = merge_fields.render(variant.body_html, data, escape=True)
    return subject, body


async def send_invitation(
    ctx: RequestContext, db: AsyncSession, conv: ScreeningConversation, disclosure: ScreeningTurn
) -> bool:
    """The templated invitation on the conversation channel (FR-290). The disclosure turn is already stored."""
    name = await owner_name(db, conv)
    sent = False
    if conv.channel == "portal_chat":
        _portal_notification(db, conv)
        sent = True
    elif conv.channel in ("link_chat", "email"):
        link = await issue_chat_link(db, conv)
        subject, body = await _render_invite(db, conv, link)
        extra = f"<p>{escape(disclosure.body)}</p>" if conv.channel == "email" else ""
        message_id = await _email(
            ctx,
            db,
            conv,
            subject,
            body + extra + f"<p><em>{escape(disclosure_line(name))}</em></p>",
            origin="agent",
        )
        disclosure.email_message_id = message_id
        sent = message_id is not None
    elif conv.channel == "whatsapp":
        await issue_chat_link(db, conv)
        sent = await _whatsapp_template(ctx, db, conv, disclosure)
    conv.invited_at = conv.invited_at or now()
    await db.flush()
    return sent


async def _whatsapp_template(
    ctx: RequestContext, db: AsyncSession, conv: ScreeningConversation, turn: ScreeningTurn
) -> bool:
    from app.schemas.messaging import RecipientRef
    from app.services.messaging import sending

    settings = await settings_row(db)
    if settings.whatsapp_invite_template_id is None:
        return False
    try:
        async with db.begin_nested():
            result = await sending.send(
                ctx, db, "whatsapp", settings.whatsapp_invite_template_id,
                [RecipientRef(entity_type="candidate", entity_id=conv.candidate_id)],
                {"candidate_id": str(conv.candidate_id), "demand_id": str(conv.demand_id)}, origin="agent",
            )  # fmt: skip
    except Exception:
        logger.info("Screening {}: WhatsApp invitation not sent", conv.id)
        return False
    first = result.results[0] if result.results else None
    if first is not None and first.message_id is not None:
        turn.channel_message_id = first.message_id
        await db.flush()
        return True
    return False


async def send_whatsapp_reminder(
    ctx: RequestContext, db: AsyncSession, conv: ScreeningConversation, turn: ScreeningTurn
) -> bool:
    """Outside the 24-hour session only approved templates may go out: the invitation template is re-sent."""
    return await _whatsapp_template(ctx, db, conv, turn)


def _portal_notification(db: AsyncSession, conv: ScreeningConversation) -> None:
    from app.core.events import emit
    from app.events.screening_agent import ScreeningChatInvited

    emit(
        db,
        ScreeningChatInvited(
            tenant_id=conv.tenant_id, conversation_id=conv.id, candidate_id=conv.candidate_id,
            application_id=conv.application_id,
        ),
    )  # fmt: skip


__all__ = [
    "company_name",
    "deliver",
    "demand_title",
    "disclosure_line",
    "issue_chat_link",
    "owner_name",
    "portal_url",
    "send_invitation",
    "send_whatsapp_reminder",
]
