# ruff: file-ignore[line-too-long]
"""Sandbox purge (Epic 79 story 79.8, spec A9).

Tenants with ``mode = 'sandbox'`` inactive for ``sandbox_inactive_purge_days`` (90) get a 14-day warning, then deletion
through ``tenant_lifecycle_service`` (the vault DEK deleted = crypto-shred). Production tenants are never touched
(guarded twice: the SQL filter and an ``is_sandbox`` re-check before any destructive call).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlalchemy import text

from app.core.database import platform_session
from app.core.settings import get_settings

if TYPE_CHECKING:
    import uuid

WARNING_DAYS = 14


async def _due(kind: str, grace_days: int) -> list[uuid.UUID]:
    """Sandbox tenant ids whose last activity is older than ``grace_days``. ``sandbox_last_active_at`` falls back to
    ``updated_at`` when the column is absent in this environment."""
    sql = (
        "SELECT id FROM tenants WHERE mode = 'sandbox' "
        "AND COALESCE(sandbox_last_active_at, updated_at) < now() - make_interval(days => :days)"
    )
    try:
        async with platform_session() as db:
            rows = await db.execute(text(sql), {"days": grace_days})
            return [r[0] for r in rows]
    except Exception:  # pragma: no cover - column absent / degrade safely
        logger.exception("sandbox_purge._due(%s) failed", kind)
        return []


async def warn(now_days: int | None = None) -> list[uuid.UUID]:
    """Return sandbox tenants that crossed the inactivity window (would receive the 14-day warning)."""
    days = now_days if now_days is not None else get_settings().sandbox_inactive_purge_days
    return await _due("warn", days)


async def purge() -> dict[str, Any]:
    """Delete sandbox tenants inactive for the window + the warning period. Production is never touched."""
    from app.core import tenancy

    days = get_settings().sandbox_inactive_purge_days + WARNING_DAYS
    due = await _due("purge", days)
    purged: list[str] = []
    for tenant_id in due:
        if not await tenancy.is_sandbox(tenant_id):  # second guard: never a production tenant
            logger.warning("sandbox_purge: %s is not a sandbox; skipped", tenant_id)
            continue
        try:
            from app.services import tenant_lifecycle_service

            await tenant_lifecycle_service.delete_tenant_data(tenant_id)
            purged.append(str(tenant_id))
        except Exception:  # pragma: no cover - lifecycle errors are logged, not fatal to the beat
            logger.exception("sandbox_purge: deletion failed for %s", tenant_id)
    return {"purged": purged, "count": len(purged)}


__all__ = ["WARNING_DAYS", "purge", "warn"]
