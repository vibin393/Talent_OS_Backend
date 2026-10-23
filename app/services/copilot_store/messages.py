# ruff: file-ignore[non-imperative-mood, line-too-long]
"""Copilot messages persistence (Epic 54). ``seq`` is per conversation; the user message of a turn carries the turn's
``run_id`` (the conversation <-> run join, coordinator decision C-1); the assistant message carries it too."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.copilot import CopilotConversation, CopilotMessage


async def add(
    db: AsyncSession,
    conv: CopilotConversation,
    *,
    seq: int,
    role: str,
    content: dict[str, Any],
    run_id: uuid.UUID | None = None,
    candidate_ids: Sequence[uuid.UUID] = (),
) -> CopilotMessage:
    row = CopilotMessage(
        id=uuid.uuid4(), tenant_id=conv.tenant_id, conversation_id=conv.id, seq=seq, role=role, content=content,
        run_id=run_id, candidate_ids=list(dict.fromkeys(candidate_ids)),
    )  # fmt: skip
    db.add(row)
    await db.flush()
    return row


async def list_after(
    db: AsyncSession, conversation_id: uuid.UUID, *, after_seq: int = 0, limit: int = 100
) -> list[CopilotMessage]:
    rows = await db.execute(
        select(CopilotMessage)
        .where(CopilotMessage.conversation_id == conversation_id, CopilotMessage.seq > after_seq)
        .order_by(CopilotMessage.seq)
        .limit(limit)
    )
    return list(rows.scalars())


async def all_of(db: AsyncSession, conversation_id: uuid.UUID) -> list[CopilotMessage]:
    return await list_after(db, conversation_id, after_seq=0, limit=100_000)


async def turn_message(db: AsyncSession, run_id: uuid.UUID, role: str = "user") -> CopilotMessage | None:
    return (
        await db.execute(
            select(CopilotMessage)
            .where(CopilotMessage.run_id == run_id, CopilotMessage.role == role)
            .order_by(CopilotMessage.seq)
            .limit(1)
        )
    ).scalar_one_or_none()


async def run_owner(db: AsyncSession, run_id: uuid.UUID) -> tuple[CopilotConversation, CopilotMessage] | None:
    """The conversation (and user message) of a Copilot turn run, or None for any other run."""
    msg = await turn_message(db, run_id, "user")
    if msg is None:
        return None
    conv = await db.get(CopilotConversation, msg.conversation_id)
    return (conv, msg) if conv is not None else None


async def run_ids(db: AsyncSession, conversation_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    if not conversation_ids:
        return []
    rows = await db.execute(
        select(CopilotMessage.run_id)
        .where(CopilotMessage.conversation_id.in_(list(conversation_ids)), CopilotMessage.run_id.is_not(None))
        .distinct()
    )
    return [r for r in rows.scalars() if r is not None]


async def draft_ids(db: AsyncSession, conversation_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """Proposal ids listed by the assistant messages (the Draft Actions of the conversations)."""
    if not conversation_ids:
        return []
    rows = await db.execute(
        select(CopilotMessage.content).where(
            CopilotMessage.conversation_id.in_(list(conversation_ids)), CopilotMessage.role == "assistant"
        )
    )
    out: list[uuid.UUID] = []
    for content in rows.scalars():
        for raw in (content or {}).get("drafts") or []:
            try:
                out.append(uuid.UUID(str(raw)))
            except ValueError:
                continue
    return list(dict.fromkeys(out))


async def last_run_id(db: AsyncSession, conversation_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await db.execute(
            select(CopilotMessage.run_id)
            .where(CopilotMessage.conversation_id == conversation_id, CopilotMessage.run_id.is_not(None))
            .order_by(CopilotMessage.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


__all__ = ["add", "all_of", "draft_ids", "last_run_id", "list_after", "run_ids", "run_owner", "turn_message"]
