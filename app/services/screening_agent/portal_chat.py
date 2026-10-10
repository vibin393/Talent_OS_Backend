# ruff: noqa: E501
"""Candidate-facing chat (Epic 57 A7): the Candidate Portal (signed-in Candidate) and the Secure-Link chat (purpose
``agent_conversation``). Both work on one conversation the caller is entitled to (another Candidate's conversation is
indistinguishable from a missing one) and return the ``PortalScreeningChat`` projection only.
"""

import dataclasses
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rate_limit, secure_links
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.models.screening_agent import ScreeningConversation
from app.schemas.screening_chat_portal import (
    PortalChatNotice,
    PortalChatTurn,
    PortalScreeningChat,
    PortalScreeningChatSummary,
)
from app.services.screening_agent import channels, conversations, inbound, turns
from app.services.screening_agent.common import CONSENT_PURPOSE, LINK_PURPOSE, conflict

MESSAGES_PER_MINUTE = 20
AUTHOR = {"agent": "assistant", "system": "assistant", "candidate": "you", "recruiter": "recruiter"}


@dataclasses.dataclass(frozen=True, slots=True)
class Subject:
    tenant_id: uuid.UUID
    conversation_id: uuid.UUID | None
    candidate_id: uuid.UUID | None  # portal: the signed-in Candidate; link: from the conversation
    via: str  # portal | link
    link_id: uuid.UUID | None = None


def portal_subject(
    tenant_id: uuid.UUID, candidate_id: uuid.UUID, conversation_id: uuid.UUID | None = None
) -> Subject:
    return Subject(tenant_id, conversation_id, candidate_id, "portal")


async def link_subject(token: str) -> Subject:
    link = await secure_links.resolve(token, (LINK_PURPOSE,))
    return Subject(link.tenant_id, link.subject_id, None, "link", link.id)


@asynccontextmanager
async def _db(subject: Subject) -> AsyncIterator[AsyncSession]:
    async with tenant_session(subject.tenant_id) as db:
        yield db


def _ctx(subject: Subject) -> RequestContext:
    return RequestContext(tenant_id=subject.tenant_id, actor_kind="system")


async def _load(db: AsyncSession, subject: Subject, *, for_update: bool = False) -> ScreeningConversation:
    stmt = select(ScreeningConversation).where(ScreeningConversation.id == subject.conversation_id)
    if subject.candidate_id is not None:
        stmt = stmt.where(ScreeningConversation.candidate_id == subject.candidate_id)
    if subject.via == "portal":
        stmt = stmt.where(ScreeningConversation.channel == "portal_chat")
    if for_update:
        stmt = stmt.with_for_update()
    conv = (await db.execute(stmt)).scalar_one_or_none()
    if conv is None or conv.invited_at is None:
        raise NotFound("Chat not found")
    if subject.via == "link" and conv.secure_link_id is not None and subject.link_id != conv.secure_link_id:
        raise NotFound("Chat not found")
    return conv


def _status(conv: ScreeningConversation) -> str:
    if conv.status == "invited":
        return "waiting_for_consent"
    if conv.status in ("consented", "in_progress", "taken_over"):
        return "open"
    if conv.status == "paused":
        return "paused"
    return "ended"


async def _notice(db: AsyncSession, tenant_id: uuid.UUID) -> PortalChatNotice:
    from app.services.privacy import consent

    row = None
    try:
        async with db.begin_nested():
            row = await consent.active_notice(db, tenant_id, CONSENT_PURPOSE)
    except Exception:
        row = None
    if row is not None:
        return PortalChatNotice(notice_id=row.id, title=row.title, body_md=row.body_md)
    title, body = consent.DEFAULT_NOTICES.get(CONSENT_PURPOSE, ("Talking with an AI assistant", ""))
    return PortalChatNotice(notice_id=None, title=title, body_md=body)


async def _project(db: AsyncSession, conv: ScreeningConversation, after_seq: int = 0) -> PortalScreeningChat:
    status = _status(conv)
    rows = await turns.list_turns(db, conv.id, after_seq)
    return PortalScreeningChat(
        id=conv.id, title=await channels.demand_title(db, conv), status=status,  # type: ignore[arg-type]
        recruiter_name=await channels.owner_name(db, conv), consent_needed=status == "waiting_for_consent",
        can_reply=status in ("open", "paused"),
        notice=await _notice(db, conv.tenant_id) if status == "waiting_for_consent" else None,
        turns=[PortalChatTurn(seq=t.seq, author=AUTHOR.get(t.author, "assistant"), body=t.body, sent_at=t.created_at) for t in rows],  # type: ignore[arg-type]
    )  # fmt: skip


async def portal_list(tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> list[PortalScreeningChatSummary]:
    async with tenant_session(tenant_id) as db:
        rows = (
            await db.execute(
                select(ScreeningConversation)
                .where(
                    ScreeningConversation.candidate_id == candidate_id,
                    ScreeningConversation.channel == "portal_chat",
                    ScreeningConversation.invited_at.is_not(None),
                )
                .order_by(ScreeningConversation.created_at.desc())
            )
        ).scalars()
        out = []
        for conv in rows:
            out.append(
                PortalScreeningChatSummary(
                    id=conv.id,
                    title=await channels.demand_title(db, conv),
                    status=_status(conv),  # type: ignore[arg-type]
                    recruiter_name=await channels.owner_name(db, conv),
                    last_message_at=max(
                        (d for d in (conv.last_inbound_at, conv.last_outbound_at) if d), default=None
                    ),
                )
            )
        return out


async def view(subject: Subject, after_seq: int = 0) -> PortalScreeningChat:
    async with _db(subject) as db:
        conv = await _load(db, subject)
        return await _project(db, conv, after_seq)


async def _limit(subject: Subject) -> None:
    key = str(subject.candidate_id or subject.link_id or subject.conversation_id)
    await rate_limit.enforce("screening_chat_messages", key, MESSAGES_PER_MINUTE, 60)


async def consent(subject: Subject, decision: str, notice_id: uuid.UUID | None) -> PortalScreeningChat:
    async with _db(subject) as db:
        conv = await _load(db, subject, for_update=True)
        if conv.status != "invited":
            raise conflict("consent_not_pending", "This chat is not waiting for your answer")
        ctx = _ctx(subject)
        if decision == "agree":
            await conversations.grant_consent(
                ctx, db, conv, evidence={"via": subject.via}, notice_id=notice_id
            )
        else:
            await conversations.refuse_consent(ctx, db, conv)
        return await _project(db, conv)


async def message(subject: Subject, body: str) -> PortalScreeningChat:
    await _limit(subject)
    async with _db(subject) as db:
        conv = await _load(db, subject, for_update=True)
        if conv.status == "invited":
            raise conflict("consent_required", 'Please choose "I agree" or "No thanks" first')
        await inbound.receive(_ctx(subject), db, conv, body)
        return await _project(db, conv)


async def handoff(subject: Subject) -> PortalScreeningChat:
    async with _db(subject) as db:
        conv = await _load(db, subject, for_update=True)
        if conv.status not in ("invited", "consented", "in_progress", "paused"):
            raise conflict("conversation_closed", "This chat has ended")
        await turns.add_turn(
            db,
            conv,
            direction="inbound",
            author="candidate",
            kind="answer",
            body="I would like to talk to a person.",
        )
        await conversations.handoff(
            _ctx(subject), db, conv, "candidate_request", 'The candidate pressed "Talk to a person".'
        )
        return await _project(db, conv)


__all__ = [
    "Subject",
    "consent",
    "handoff",
    "link_subject",
    "message",
    "portal_list",
    "portal_subject",
    "view",
]
