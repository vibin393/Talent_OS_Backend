# ruff: noqa: E501, D401
"""Candidate messages into a conversation (Epic 57 A3; portal chat, Secure-Link chat, email replies, WhatsApp).

Order (spec): store the turn (+ transcript) -> consent keywords while consent is pending (email / WhatsApp, no model
call) -> stop words / opt-out (hand-off without a model call) -> paused / taken over (stored, unanswered) -> LL144
alternative process -> language (a second non-English message hands off) -> ``wake`` (resume the waiting run with the
text as untrusted input, or a continuation run).
"""

import re
import unicodedata
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.screening_agent import ScreeningConversation, ScreeningTurn
from app.services.screening_agent import conversations, turns
from app.services.screening_agent.common import (
    CONSENT_NO,
    CONSENT_YES,
    DEFAULT_STOP_WORDS,
    WA_WINDOW_HOURS,
    conflict,
    is_open,
    now,
    settings_row,
)

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
CHAT_CONSENT_HINT = (
    'Please choose "I agree" to start, or "No thanks" if you prefer not to chat with the AI assistant.'
)


def _norm(text: str) -> str:
    return " ".join(_PUNCT.sub(" ", unicodedata.normalize("NFKC", text).lower()).split())


def is_stop(text: str, stop_words: list[str]) -> bool:
    norm = _norm(text)
    if not norm:
        return False
    for word in stop_words or DEFAULT_STOP_WORDS:
        w = _norm(word)
        if not w:
            continue
        if norm == w or (" " in w and w in norm):
            return True
    return False


def consent_keyword(text: str) -> str | None:
    norm = _norm(text)
    if norm in CONSENT_YES:
        return "yes"
    if norm in CONSENT_NO:
        return "no"
    return None


def non_english(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 3:
        return False
    foreign = sum(1 for c in letters if ord(c) > 0x024F)
    return foreign / len(letters) > 0.5


async def receive(
    ctx: RequestContext,
    db: AsyncSession,
    conv: ScreeningConversation,
    body: str,
    *,
    channel_message_id: uuid.UUID | None = None,
    email_message_id: uuid.UUID | None = None,
) -> tuple[ScreeningTurn, str]:
    """Process one candidate message. The caller holds the conversation row lock. Returns ``(turn, outcome)``."""
    if not is_open(conv) and conv.status != "taken_over":
        raise conflict("conversation_closed", "This chat has ended")
    turn = await turns.add_turn(db, conv, direction="inbound", author="candidate", kind="answer", body=body)
    turn.channel_message_id = channel_message_id
    turn.email_message_id = email_message_id
    if conv.channel == "whatsapp":
        conv.wa_window_until = now() + timedelta(hours=WA_WINDOW_HOURS)
    settings = await settings_row(db)
    if conv.status == "invited":
        keyword = consent_keyword(body) if conv.channel in ("email", "whatsapp") else None
        if keyword == "yes":
            await conversations.grant_consent(
                ctx, db, conv, evidence={"turn_id": str(turn.id), "channel": conv.channel}
            )
            return turn, "consent_granted"
        if keyword == "no" or is_stop(body, list(settings.stop_words or [])):
            await conversations.refuse_consent(ctx, db, conv)
            return turn, "consent_refused"
        if conv.channel in ("portal_chat", "link_chat"):
            await turns.add_turn(
                db,
                conv,
                direction="outbound",
                author="system",
                kind="acknowledgement",
                body=CHAT_CONSENT_HINT,
                template_key="consent_hint",
            )
        return turn, "awaiting_consent"
    if is_stop(body, list(settings.stop_words or [])):
        if conv.status == "taken_over":
            return turn, "stored"
        await conversations.handoff(
            ctx, db, conv, "candidate_request", "The candidate asked to stop or to talk to a person."
        )
        return turn, "handed_off"
    if conv.status in ("paused", "taken_over"):
        return turn, "stored"
    if await conversations.alternative_process_requested(db, conv.candidate_id, conv.demand_id):
        await conversations.end_for_alternative_process(ctx, db, conv)
        return turn, "alternative_process"
    if non_english(body):
        conv.non_english_count += 1
        if conv.non_english_count >= 2:
            await conversations.handoff(
                ctx, db, conv, "language", "The candidate wrote in a language other than English."
            )
            return turn, "handed_off"
    outcome = await conversations.wake(ctx, db, conv, reply=body)
    return turn, outcome


async def on_channel_inbound(ctx: RequestContext, db: AsyncSession, message_id: uuid.UUID) -> str | None:
    """A matched inbound WhatsApp / SMS message of a Candidate with an open WhatsApp conversation."""
    from app.models.comms import ChannelMessage

    msg = await db.get(ChannelMessage, message_id)
    if msg is None or msg.direction != "inbound" or msg.candidate_id is None:
        return None
    if (
        await db.execute(select(ScreeningTurn.id).where(ScreeningTurn.channel_message_id == message_id))
    ).first() is not None:
        return "duplicate"
    conv = await conversations.open_for_candidate(db, msg.candidate_id, "whatsapp")
    if conv is None or not (msg.body_rendered or "").strip():
        return None
    _, outcome = await receive(ctx, db, conv, msg.body_rendered, channel_message_id=msg.id)
    return outcome


async def on_email_reply(
    ctx: RequestContext,
    db: AsyncSession,
    inbound_email_id: uuid.UUID,
    email_message_id: uuid.UUID,
    candidate_id: uuid.UUID | None,
) -> str | None:
    """A captured email reply in the thread of a conversation email (or from the conversation's Candidate)."""
    from app.models.comms import InboundEmail

    turn = (
        await db.execute(
            select(ScreeningTurn).where(ScreeningTurn.email_message_id == email_message_id).limit(1)
        )
    ).scalar_one_or_none()
    conv = None
    if turn is not None:
        conv = (
            await db.execute(
                select(ScreeningConversation)
                .where(ScreeningConversation.id == turn.conversation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
    elif candidate_id is not None:
        conv = await conversations.open_for_candidate(db, candidate_id, "email")
    if conv is None or conv.channel != "email":
        return None
    inbound = await db.get(InboundEmail, inbound_email_id)
    text = (inbound.body_text or "").strip() if inbound is not None else ""
    if not text:
        return None
    if not is_open(conv) and conv.status != "taken_over":
        return "closed"
    _, outcome = await receive(ctx, db, conv, text[:2000])
    return outcome


__all__ = [
    "CHAT_CONSENT_HINT",
    "consent_keyword",
    "is_stop",
    "non_english",
    "on_channel_inbound",
    "on_email_reply",
    "receive",
]
