# ruff: noqa: E501
"""Portal Copilot persistence (Epic 77 A1): conversations, sealed messages, feedback.

Every function takes an **internal** tenant session (the vault data key is internal-only) and filters by the owning
portal principal explicitly (``principal_type`` + ``principal_id`` + its org column): a conversation of anyone else is
404, exactly as if it did not exist. The tables also carry org RLS, so a portal-scoped session sees only its own
organization's rows (defence in depth). Content is sealed with ``vault_service.seal_field`` under the context
``portal_copilot_messages.content:<id>`` (titles ``portal_copilot_conversations.title:<id>``, feedback comments
``portal_copilot_feedback.comment:<id>``); logs never contain it.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.models.portal_copilot import PortalCopilotConversation, PortalCopilotFeedback, PortalCopilotMessage
from app.services import vault_service
from app.services.portal_copilot.principals import PortalActor

TITLE_CHARS = 60


def _now() -> datetime:
    return datetime.now(UTC)


def msg_context(message_id: uuid.UUID) -> str:
    return f"portal_copilot_messages.content:{message_id}"


def title_context(conversation_id: uuid.UUID) -> str:
    return f"portal_copilot_conversations.title:{conversation_id}"


def comment_context(feedback_id: uuid.UUID) -> str:
    return f"portal_copilot_feedback.comment:{feedback_id}"


def _owned(stmt: Any, model: Any, actor: PortalActor) -> Any:
    cols = actor.org_columns()
    stmt = stmt.where(model.principal_type == actor.principal_type, model.principal_id == actor.principal_id)
    for name, value in cols.items():
        if value is not None:
            stmt = stmt.where(getattr(model, name) == value)
    return stmt


# ================================================================ conversations
async def create_conversation(
    db: AsyncSession, actor: PortalActor, *, retention_days: int
) -> PortalCopilotConversation:
    conv = PortalCopilotConversation(
        id=uuid.uuid4(), tenant_id=actor.tenant_id, principal_type=actor.principal_type, principal_id=actor.principal_id,
        agent_key=actor.agent_key, status="open", message_count=0, expires_at=_now() + timedelta(days=retention_days),
        **actor.org_columns(),
    )  # fmt: skip
    db.add(conv)
    await db.flush()
    return conv


async def owned_conversation(
    db: AsyncSession, actor: PortalActor, conversation_id: uuid.UUID, *, lock: bool = False
) -> PortalCopilotConversation:
    stmt = _owned(select(PortalCopilotConversation), PortalCopilotConversation, actor).where(
        PortalCopilotConversation.id == conversation_id
    )
    if lock:
        stmt = stmt.with_for_update()
    conv = (await db.execute(stmt)).scalar_one_or_none()
    if conv is None:
        raise NotFound("Conversation not found")
    return conv


async def list_conversations(
    db: AsyncSession, actor: PortalActor, *, page: int, page_size: int
) -> tuple[list[PortalCopilotConversation], int]:
    base = _owned(select(PortalCopilotConversation), PortalCopilotConversation, actor)
    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
    rows = (
        await db.execute(
            base.order_by(
                PortalCopilotConversation.last_message_at.desc().nulls_last(),
                PortalCopilotConversation.created_at.desc(),
            )
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).scalars()
    return list(rows), total


def touch(conv: PortalCopilotConversation, *, retention_days: int) -> int:
    conv.message_count = int(conv.message_count or 0) + 1
    conv.last_message_at = _now()
    conv.expires_at = _now() + timedelta(days=retention_days)
    return conv.message_count


async def set_title(db: AsyncSession, conv: PortalCopilotConversation, text: str) -> None:
    conv.title_sealed = await vault_service.seal_field(
        db, conv.tenant_id, title_context(conv.id), text[:TITLE_CHARS]
    )


async def title_of(db: AsyncSession, conv: PortalCopilotConversation) -> str | None:
    if conv.title_sealed is None:
        return None
    return await vault_service.open_field(
        db, conv.tenant_id, title_context(conv.id), bytes(conv.title_sealed)
    )


# ================================================================ messages
async def add_message(
    db: AsyncSession,
    conv: PortalCopilotConversation,
    *,
    seq: int,
    role: str,
    content: dict[str, Any],
    run_id: uuid.UUID | None = None,
) -> PortalCopilotMessage:
    mid = uuid.uuid4()
    sealed = await vault_service.seal_field(
        db, conv.tenant_id, msg_context(mid), json.dumps(content, default=str)
    )
    msg = PortalCopilotMessage(
        id=mid, tenant_id=conv.tenant_id, conversation_id=conv.id, client_id=conv.client_id, vendor_id=conv.vendor_id,
        candidate_id=conv.candidate_id, seq=seq, role=role, content_sealed=sealed, run_id=run_id,
    )  # fmt: skip
    db.add(msg)
    await db.flush()
    return msg


async def content_of(db: AsyncSession, msg: PortalCopilotMessage) -> dict[str, Any]:
    if msg.redacted_at is not None:
        return {"text": "", "redacted": True}
    raw = await vault_service.open_field(db, msg.tenant_id, msg_context(msg.id), bytes(msg.content_sealed))
    value = json.loads(raw)
    return value if isinstance(value, dict) else {"text": str(value)}


async def messages_of(db: AsyncSession, conversation_id: uuid.UUID) -> list[PortalCopilotMessage]:
    return list(
        (
            await db.execute(
                select(PortalCopilotMessage)
                .where(PortalCopilotMessage.conversation_id == conversation_id)
                .order_by(PortalCopilotMessage.seq)
            )
        ).scalars()
    )


async def turn_of_run(
    db: AsyncSession, run_id: uuid.UUID
) -> tuple[PortalCopilotConversation, PortalCopilotMessage] | None:
    """(conversation, user message) of a portal turn run."""
    row = (
        await db.execute(
            select(PortalCopilotConversation, PortalCopilotMessage)
            .join(PortalCopilotMessage, PortalCopilotMessage.conversation_id == PortalCopilotConversation.id)
            .where(PortalCopilotMessage.run_id == run_id, PortalCopilotMessage.role == "user")
        )
    ).first()
    return (row[0], row[1]) if row is not None else None


async def answer_of_run(db: AsyncSession, run_id: uuid.UUID) -> PortalCopilotMessage | None:
    return (
        await db.execute(
            select(PortalCopilotMessage).where(
                PortalCopilotMessage.run_id == run_id, PortalCopilotMessage.role == "assistant"
            )
        )
    ).scalar_one_or_none()


# ================================================================ feedback
async def upsert_feedback(
    db: AsyncSession,
    conv: PortalCopilotConversation,
    actor: PortalActor,
    run_id: uuid.UUID,
    *,
    rating: str,
    reason: str | None,
    comment: str | None,
) -> PortalCopilotFeedback:
    row = (
        await db.execute(
            select(PortalCopilotFeedback).where(
                PortalCopilotFeedback.run_id == run_id,
                PortalCopilotFeedback.principal_id == actor.principal_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = PortalCopilotFeedback(
            id=uuid.uuid4(), tenant_id=actor.tenant_id, run_id=run_id, conversation_id=conv.id,
            principal_type=actor.principal_type, principal_id=actor.principal_id, **actor.org_columns(),
            rating=rating,
        )  # fmt: skip
        db.add(row)
    row.rating, row.reason = rating, reason
    row.comment_sealed = (
        await vault_service.seal_field(db, actor.tenant_id, comment_context(row.id), comment[:500])
        if comment
        else None
    )
    await db.flush()
    return row


# ================================================================ removal
async def delete_conversations(db: AsyncSession, ids: list[uuid.UUID]) -> int:
    if not ids:
        return 0
    result = await db.execute(delete(PortalCopilotConversation).where(PortalCopilotConversation.id.in_(ids)))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def expired_ids(db: AsyncSession, now: datetime | None = None) -> list[uuid.UUID]:
    return list(
        (
            await db.execute(
                select(PortalCopilotConversation.id).where(
                    PortalCopilotConversation.expires_at <= (now or _now())
                )
            )
        ).scalars()
    )


async def ids_of_principal(db: AsyncSession, principal_type: str, principal_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await db.execute(
                select(PortalCopilotConversation.id).where(
                    PortalCopilotConversation.principal_type == principal_type,
                    PortalCopilotConversation.principal_id == principal_id,
                )
            )
        ).scalars()
    )


async def remaining_for_candidate(db: AsyncSession, candidate_id: uuid.UUID) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for model in (PortalCopilotConversation, PortalCopilotMessage, PortalCopilotFeedback):
        out.extend((await db.execute(select(model.id).where(model.candidate_id == candidate_id))).scalars())
    return out


__all__ = [
    "add_message",
    "answer_of_run",
    "comment_context",
    "content_of",
    "create_conversation",
    "delete_conversations",
    "expired_ids",
    "ids_of_principal",
    "list_conversations",
    "messages_of",
    "msg_context",
    "owned_conversation",
    "remaining_for_candidate",
    "set_title",
    "title_context",
    "title_of",
    "touch",
    "turn_of_run",
    "upsert_feedback",
]
