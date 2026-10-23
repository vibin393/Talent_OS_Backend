# ruff: file-ignore[line-too-long]
"""Retention of ``mcp_sessions`` metadata (``mcp_session_retention_days``; payloads follow the P4 retention)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp import McpAccessHold, McpSession


async def purge_older_than(db: AsyncSession, days: int) -> int:
    """Delete ended sessions older than ``days`` (holds keep their row; the session link is cleared)."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    ids = list(
        (await db.execute(select(McpSession.id).where(McpSession.status == "ended", McpSession.ended_at < cutoff))).scalars()
    )
    if not ids:
        return 0
    from sqlalchemy import update

    await db.execute(update(McpAccessHold).where(McpAccessHold.session_id.in_(ids)).values(session_id=None))
    await db.execute(delete(McpSession).where(McpSession.id.in_(ids)))
    return len(ids)


__all__ = ["purge_older_than"]
