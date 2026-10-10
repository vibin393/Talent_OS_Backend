# ruff: noqa: E501, D401
"""``mcp_settings`` (one row per tenant; Epic 73, spec A2)."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp import McpSettings

DEFAULTS: dict[str, Any] = {
    "calls_per_minute": None,
    "calls_per_user_day": 2000,
    "proposals_per_hour": 25,
    "bulk_read_threshold": 500,
    "max_results": 50,
    "allowed_origins": [],
    "notify_user_on_first_connection": True,
}
FIELDS = tuple(DEFAULTS)


async def get(db: AsyncSession, *, lock: bool = False) -> McpSettings | None:
    stmt = select(McpSettings)
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def values(db: AsyncSession) -> dict[str, Any]:
    """The effective settings (the defaults when the tenant has no row)."""
    row = await get(db)
    if row is None:
        return dict(DEFAULTS)
    return {k: getattr(row, k) for k in FIELDS}


async def ensure(db: AsyncSession, tenant_id: uuid.UUID) -> McpSettings:
    row = await get(db, lock=True)
    if row is None:
        row = McpSettings(tenant_id=tenant_id, **{k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()})
        db.add(row)
        await db.flush()
    return row


async def upsert(db: AsyncSession, tenant_id: uuid.UUID, changes: dict[str, Any], *, by: uuid.UUID | None) -> McpSettings:
    row = await ensure(db, tenant_id)
    for key, value in changes.items():
        if key in FIELDS:
            setattr(row, key, value)
    row.updated_by = by
    await db.flush()
    return row


__all__ = ["DEFAULTS", "FIELDS", "ensure", "get", "upsert", "values"]
