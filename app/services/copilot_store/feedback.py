# ruff: noqa: E501
"""Copilot feedback persistence (Epic 54): one rating per (run, user), upserted. Feedback is not a decision: no
``ai_actions.decision`` write-back (Epic 65 reports it)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.copilot import CopilotFeedback


async def upsert(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    user_id: uuid.UUID,
    agent_key: str,
    rating: str,
    comment: str | None,
) -> CopilotFeedback:
    row = (
        await db.execute(
            select(CopilotFeedback)
            .where(CopilotFeedback.run_id == run_id, CopilotFeedback.user_id == user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = CopilotFeedback(
            id=uuid.uuid4(), tenant_id=tenant_id, run_id=run_id, conversation_id=conversation_id, user_id=user_id,
            agent_key=agent_key, rating=rating, comment=comment,
        )  # fmt: skip
        db.add(row)
    else:
        row.rating = rating
        row.comment = comment
        row.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(row)
    return row


__all__ = ["upsert"]
