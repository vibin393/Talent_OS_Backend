# ruff: noqa: E501, D401
"""``mcp_sessions`` rows (Epic 73, spec A2 / A3)."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp import McpSession

COUNTERS = ("call_count", "proposal_count", "refusal_count", "violation_count", "redaction_count")


def _now() -> datetime:
    return datetime.now(UTC)


async def create(
    db: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID, installation_id: uuid.UUID, user_id: uuid.UUID,
    token_family_id: uuid.UUID | None, session_hash: bytes, protocol_version: str, client_name: str,
    client_version: str | None,
) -> McpSession:  # fmt: skip
    now = _now()
    row = McpSession(
        id=uuid.uuid4(), tenant_id=tenant_id, run_id=run_id, installation_id=installation_id, user_id=user_id,
        token_family_id=token_family_id, session_hash=session_hash, protocol_version=protocol_version,
        client_name=client_name[:120], client_version=(client_version or None) and client_version[:40], status="active",
        started_at=now, last_seen_at=now,
    )  # fmt: skip
    db.add(row)
    await db.flush()
    return row


async def get_by_hash(db: AsyncSession, session_hash: bytes) -> McpSession | None:
    return (await db.execute(select(McpSession).where(McpSession.session_hash == session_hash))).scalar_one_or_none()


async def get(db: AsyncSession, session_id: uuid.UUID, *, lock: bool = False) -> McpSession | None:
    stmt = select(McpSession).where(McpSession.id == session_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def touch(db: AsyncSession, row: McpSession, **deltas: int) -> None:
    """``last_seen_at`` = now and the counters incremented by ``deltas`` (``call_count=1`` ...)."""
    values: dict[str, Any] = {"last_seen_at": _now()}
    for key, delta in deltas.items():
        if key in COUNTERS and delta:
            values[key] = getattr(McpSession, key) + delta
    await db.execute(update(McpSession).where(McpSession.id == row.id).values(**values))


async def set_distinct(db: AsyncSession, row: McpSession, count: int) -> None:
    await db.execute(
        update(McpSession)
        .where(McpSession.id == row.id)
        .values(distinct_candidates=func.greatest(McpSession.distinct_candidates, count))
    )


async def end(db: AsyncSession, row: McpSession, *, reason: str, status: str = "ended") -> bool:
    """End (or pause) an active session; False when it was already not active."""
    if row.status != "active":
        return False
    row.status = status
    row.end_reason = reason
    row.ended_at = _now() if status == "ended" else None
    await db.flush()
    return True


async def active(
    db: AsyncSession, *, installation_ids: Sequence[uuid.UUID] | None = None, user_id: uuid.UUID | None = None,
    app_id: uuid.UUID | None = None,
) -> list[McpSession]:  # fmt: skip
    """Active sessions of the tenant, narrowed by Installation(s), user and / or App (through ``app_installations``)."""
    stmt = select(McpSession).where(McpSession.status == "active")
    if installation_ids is not None:
        stmt = stmt.where(McpSession.installation_id.in_(list(installation_ids)))
    if user_id is not None:
        stmt = stmt.where(McpSession.user_id == user_id)
    if app_id is not None:
        ids = [
            r[0]
            for r in (
                await db.execute(text("SELECT id FROM app_installations WHERE app_id = CAST(:a AS uuid)"), {"a": str(app_id)})
            ).all()
        ]
        stmt = stmt.where(McpSession.installation_id.in_(ids))
    return list((await db.execute(stmt.with_for_update(skip_locked=True))).scalars())


async def stale_active(db: AsyncSession) -> list[tuple[McpSession, str | None]]:
    """Active sessions whose runtime run is no longer running (idle close, deadline, kill switch): (row, run stop reason)."""
    rows = (
        await db.execute(
            text(
                "SELECT s.id, r.status, r.reason_code FROM mcp_sessions s JOIN agent_runs r ON r.id = s.run_id "
                "WHERE s.status = 'active' AND r.status <> 'running'"
            )
        )
    ).all()
    out: list[tuple[McpSession, str | None]] = []
    for sid, _status, reason in rows:
        row = await get(db, sid, lock=True)
        if row is not None and row.status == "active":
            out.append((row, reason))
    return out


async def run_reason(db: AsyncSession, run_id: uuid.UUID) -> tuple[str | None, str | None]:
    """(status, stop reason) of the session's runtime run."""
    row = (
        await db.execute(text("SELECT status, reason_code FROM agent_runs WHERE id = CAST(:r AS uuid)"), {"r": str(run_id)})
    ).first()
    return (row[0], row[1]) if row is not None else (None, None)


async def first_for(db: AsyncSession, *, user_id: uuid.UUID, installation_id: uuid.UUID) -> bool:
    """True when this (user, Installation) has exactly one session row (the one just created)."""
    count = (
        await db.execute(
            select(func.count()).select_from(McpSession).where(
                McpSession.user_id == user_id, McpSession.installation_id == installation_id
            )
        )
    ).scalar_one()
    return int(count) == 1


async def list_(
    db: AsyncSession, *, status: str | None, user_id: uuid.UUID | None, installation_id: uuid.UUID | None,
    started_from: datetime | None, started_to: datetime | None, page: int, page_size: int,
) -> tuple[list[McpSession], int]:  # fmt: skip
    stmt = select(McpSession)
    if status is not None:
        stmt = stmt.where(McpSession.status == status)
    if user_id is not None:
        stmt = stmt.where(McpSession.user_id == user_id)
    if installation_id is not None:
        stmt = stmt.where(McpSession.installation_id == installation_id)
    if started_from is not None:
        stmt = stmt.where(McpSession.started_at >= started_from)
    if started_to is not None:
        stmt = stmt.where(McpSession.started_at <= started_to)
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (
        await db.execute(
            stmt.order_by(McpSession.started_at.desc(), McpSession.id).limit(page_size).offset((page - 1) * page_size)
        )
    ).scalars()
    return list(rows), total


__all__ = [
    "COUNTERS", "active", "create", "end", "first_for", "get", "get_by_hash", "list_", "run_reason", "set_distinct",
    "stale_active", "touch",
]  # fmt: skip
