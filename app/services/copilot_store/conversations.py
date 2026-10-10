# ruff: noqa: E501
"""Copilot conversations persistence (Epic 54). Every read is filtered on the owner (``user_id``): a conversation of
another user is "not found" -- admins included (coordinator decision 6)."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.core.pagination import Page, PageParams, paginate
from app.models.copilot import CopilotConversation, CopilotFeedback


def _now() -> datetime:
    return datetime.now(UTC)


async def create(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    agent_key: str,
    context_ref: dict[str, Any] | None,
    retention_days: int,
) -> CopilotConversation:
    row = CopilotConversation(
        id=uuid.uuid4(), tenant_id=tenant_id, user_id=user_id, agent_key=agent_key, title=None, title_locked=False,
        status="open", context_ref=context_ref, message_count=0, expires_at=_now() + timedelta(days=retention_days),
    )  # fmt: skip
    db.add(row)
    await db.flush()
    return row


async def owned(
    db: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False
) -> CopilotConversation:
    stmt = select(CopilotConversation).where(
        CopilotConversation.id == conversation_id, CopilotConversation.user_id == user_id
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Conversation not found")
    return row


async def get(db: AsyncSession, conversation_id: uuid.UUID) -> CopilotConversation | None:
    return await db.get(CopilotConversation, conversation_id)


async def list_owned(
    db: AsyncSession, user_id: uuid.UUID, *, status: str | None, page: int, page_size: int
) -> Page[Any]:
    stmt = select(CopilotConversation).where(CopilotConversation.user_id == user_id)
    if status:
        stmt = stmt.where(CopilotConversation.status == status)
    stmt = stmt.order_by(
        func.coalesce(CopilotConversation.last_message_at, CopilotConversation.created_at).desc(),
        CopilotConversation.id,
    )
    return await paginate(db, stmt, PageParams(page=page, page_size=page_size))


async def count_open(db: AsyncSession, user_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(CopilotConversation)
                .where(CopilotConversation.user_id == user_id, CopilotConversation.status == "open")
            )
        ).scalar_one()
    )


def touch(conv: CopilotConversation, *, retention_days: int, added: int = 1) -> int:
    """Advance the counters; returns the next ``seq``."""
    conv.message_count = (conv.message_count or 0) + added
    conv.last_message_at = _now()
    conv.expires_at = _now() + timedelta(days=retention_days)
    return conv.message_count


def close(conv: CopilotConversation) -> None:
    conv.status = "closed"
    conv.closed_at = _now()


async def delete_rows(db: AsyncSession, conversation_ids: Sequence[uuid.UUID]) -> int:
    """Delete conversations (messages cascade); feedback keeps its rating with the conversation reference nulled."""
    if not conversation_ids:
        return 0
    await db.execute(
        update(CopilotFeedback)
        .where(CopilotFeedback.conversation_id.in_(list(conversation_ids)))
        .values(conversation_id=None)
    )
    result = await db.execute(
        delete(CopilotConversation).where(CopilotConversation.id.in_(list(conversation_ids)))
    )
    await db.flush()
    return int(getattr(result, "rowcount", 0) or 0)


async def expired(db: AsyncSession, *, limit: int = 500) -> list[CopilotConversation]:
    rows = await db.execute(
        select(CopilotConversation)
        .where(CopilotConversation.expires_at < _now())
        .order_by(CopilotConversation.expires_at)
        .limit(limit)
    )
    return list(rows.scalars())


__all__ = ["close", "count_open", "create", "delete_rows", "expired", "get", "list_owned", "owned", "touch"]
