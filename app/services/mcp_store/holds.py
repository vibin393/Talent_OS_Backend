# ruff: file-ignore[non-imperative-mood]
"""``mcp_access_holds`` rows (Epic 73, spec 0.6): a user's MCP access paused until released."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp import McpAccessHold


def _now() -> datetime:
    return datetime.now(UTC)


async def open_(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    installation_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    reason: str,
    auto_release_hours: int,
) -> McpAccessHold | None:
    """Open a hold (no-op when one is already open for the user). Returns the open hold."""
    now = _now()
    expired = (
        await db.execute(
            select(McpAccessHold).where(
                McpAccessHold.user_id == user_id,
                McpAccessHold.released_at.is_(None),
                McpAccessHold.auto_release_at <= now,
            )
        )
    ).scalar_one_or_none()
    if expired is not None:
        expired.released_at = expired.auto_release_at
        await db.flush()
    current = await active_for(db, user_id)
    if current is not None:
        return current
    row = McpAccessHold(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        installation_id=installation_id,
        session_id=session_id,
        reason=reason,
        held_at=now,
        auto_release_at=now + timedelta(hours=auto_release_hours) if auto_release_hours > 0 else None,
    )
    db.add(row)
    await db.flush()
    return row


async def active_for(db: AsyncSession, user_id: uuid.UUID) -> McpAccessHold | None:
    """The user's open hold (an auto-release time in the past counts as released)."""
    row = (
        await db.execute(
            select(McpAccessHold).where(McpAccessHold.user_id == user_id, McpAccessHold.released_at.is_(None))
        )
    ).scalar_one_or_none()
    if row is not None and row.auto_release_at is not None and row.auto_release_at <= _now():
        return None
    return row


async def get(db: AsyncSession, hold_id: uuid.UUID) -> McpAccessHold | None:
    return (
        await db.execute(select(McpAccessHold).where(McpAccessHold.id == hold_id).with_for_update())
    ).scalar_one_or_none()


async def list_open(db: AsyncSession) -> list[McpAccessHold]:
    return list(
        (
            await db.execute(
                select(McpAccessHold).where(McpAccessHold.released_at.is_(None)).order_by(McpAccessHold.held_at.desc())
            )
        ).scalars()
    )


async def release(
    db: AsyncSession, row: McpAccessHold, *, by: uuid.UUID | None, note: str | None
) -> McpAccessHold:
    row.released_at = _now()
    row.released_by = by
    if note:
        row.note = note[:500]
    await db.flush()
    return row


async def auto_release_due(db: AsyncSession) -> list[McpAccessHold]:
    """Close holds whose auto-release time has passed; returns them."""
    rows = list(
        (
            await db.execute(
                select(McpAccessHold)
                .where(
                    McpAccessHold.released_at.is_(None),
                    McpAccessHold.auto_release_at.is_not(None),
                    McpAccessHold.auto_release_at <= _now(),
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for row in rows:
        row.released_at = row.auto_release_at
    await db.flush()
    return rows


__all__ = ["active_for", "auto_release_due", "get", "list_open", "open_", "release"]
