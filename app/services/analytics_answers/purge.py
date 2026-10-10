# ruff: noqa: E501, D400, D415
"""Retention of analytics answers (Epic 64, A8). Daily on ``maintenance`` (``analytics_answers.purge_expired``):

* answers past ``expires_at`` (created + 90 days) are deleted;
* answers whose ``conversation_id`` no longer exists in ``copilot_conversations`` (Epic 54; read by UUID through a
  lightweight table reference, no FK, no import of Copilot code) are deleted -- a conversation delete / purge is
  followed within 24 h. Skipped while that table does not exist.
"""

from datetime import UTC, datetime

from sqlalchemy import column, delete, exists, func, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics_answers import AnalyticsAnswer

__all__ = ["purge_expired"]

_conversations = table("copilot_conversations", column("id"))


async def _conversations_exist(db: AsyncSession) -> bool:
    return (await db.execute(select(func.to_regclass("copilot_conversations")))).scalar() is not None


async def purge_expired(db: AsyncSession, at: datetime | None = None) -> int:
    at = at or datetime.now(UTC)
    result = await db.execute(delete(AnalyticsAnswer).where(AnalyticsAnswer.expires_at < at))
    count = int(getattr(result, "rowcount", 0) or 0)
    if await _conversations_exist(db):
        orphan = ~exists(
            select(_conversations.c.id).where(_conversations.c.id == AnalyticsAnswer.conversation_id)
        )
        result = await db.execute(
            delete(AnalyticsAnswer).where(AnalyticsAnswer.conversation_id.is_not(None), orphan)
        )
        count += int(getattr(result, "rowcount", 0) or 0)
    await db.flush()
    return count
