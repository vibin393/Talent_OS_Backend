# ruff: noqa: E501, D401
"""Ecosystem suspension state (Epic 66, AD-86, FR-340).

``is_suspended(tenant_id, app_id=None)`` is the one check used by ``oauth.authenticate_bearer``, API keys on the Public
API, connector jobs (outbound paused / inbound held), webhook sinks and deliveries (68) and MCP (73). It reads, in order:
the platform global suspension (``platform_ecosystem_controls``), the platform per-tenant suspension and the tenant's own
Ecosystem Kill Switch (``ecosystem_settings``), and the App's runtime status (``apps.status = 'suspended'``). Results
are cached per process for ``ecosystem_state_cache_seconds`` (15 s) so every effect lands within 60 s; a change made in
this process busts the cache at once.
"""

import time
import uuid

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import platform_session, tenant_session
from app.core.settings import get_settings
from app.models.ecosystem import EcosystemSettings
from app.schemas.ecosystem import SuspendState

_cache: dict[tuple[str, str | None], tuple[SuspendState, float]] = {}


def bust_cache(tenant_id: uuid.UUID | None = None) -> None:
    """Drop cached states (``tenant_id`` None = all tenants: global / App changes)."""
    if tenant_id is None:
        _cache.clear()
        return
    for key in [k for k in _cache if k[0] == str(tenant_id)]:
        _cache.pop(key, None)


async def settings_row(db: AsyncSession, tenant_id: uuid.UUID, *, lock: bool = False) -> EcosystemSettings:
    """The tenant's ``ecosystem_settings`` row (lazily created)."""
    stmt = select(EcosystemSettings).where(EcosystemSettings.tenant_id == tenant_id)
    row = (await db.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    if row is None:
        await db.execute(
            pg_insert(EcosystemSettings)
            .values(id=uuid.uuid4(), tenant_id=tenant_id)
            .on_conflict_do_nothing(index_elements=["tenant_id"])
        )
        row = (await db.execute(stmt.with_for_update() if lock else stmt)).scalar_one()
    return row


async def _compute(tenant_id: uuid.UUID, app_id: uuid.UUID | None) -> SuspendState:
    async with platform_session() as pdb:
        glob = (
            await pdb.execute(
                text(
                    "SELECT global_suspended_at, global_suspended_reason FROM platform_ecosystem_controls WHERE id = 1"
                )
            )
        ).first()
        app_row = None
        if app_id is not None:
            app_row = (
                await pdb.execute(
                    text("SELECT status, suspended_reason FROM apps WHERE id = CAST(:a AS uuid)"),
                    {"a": str(app_id)},
                )
            ).first()
    if glob is not None and glob[0] is not None:
        return SuspendState(suspended=True, scope="global", reason=glob[1])
    async with tenant_session(tenant_id) as db:
        row = (
            await db.execute(select(EcosystemSettings).where(EcosystemSettings.tenant_id == tenant_id))
        ).scalar_one_or_none()
    if row is not None and row.platform_suspended_at is not None:
        return SuspendState(suspended=True, scope="platform_tenant", reason=row.platform_suspended_reason)
    if row is not None and row.kill_switch_at is not None:
        return SuspendState(suspended=True, scope="tenant", reason=row.kill_switch_reason)
    if app_row is not None and app_row[0] in ("suspended", "retired"):
        return SuspendState(suspended=True, scope="platform_app", reason=app_row[1])
    return SuspendState(suspended=False)


async def is_suspended(tenant_id: uuid.UUID, *, app_id: uuid.UUID | None = None) -> SuspendState:
    key = (str(tenant_id), str(app_id) if app_id else None)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[1] > now:
        return hit[0]
    state = await _compute(tenant_id, app_id)
    _cache[key] = (state, now + get_settings().ecosystem_state_cache_seconds)
    return state


__all__ = ["bust_cache", "is_suspended", "settings_row"]
