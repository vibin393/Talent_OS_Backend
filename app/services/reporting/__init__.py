"""Reporting service facade (Epic 11: dashboards, report lists, exports) -- ``reporting_service``.

Public API (routers, tasks and other services import from here):

* dashboards: ``list_dashboards``, ``get_tile``;
* report lists: ``list_report`` (``/reports/lists/{kind}``);
* exports: ``request_export``, ``list_exports``, ``get_export``, ``download_export``, ``run_export``,
  ``purge_expired_exports``;
* SM-3 bookkeeping: ``touch_active_day`` (principal resolver), ``purge_active_days`` (beat).

Modules: ``common`` (read-model projections, scope, time), ``filters``, ``dashboards`` (tile registry),
``metrics/{recruiter,manager,account}``, ``lists`` (list-view query builders), ``export``.
See ``common`` for the documented AD-1 read-model exception (read-only SQL over other domains' tables).
"""

import threading
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal
from app.models.reporting import UserActiveDay
from app.schemas.reporting import ExportCreate, ExportOut
from app.services.reporting import (
    common,
    dashboards,
    export,
    filters,
    lists,
    lists_p2,
    lists_p3,  # ruff: ignore[unused-import] - Epic 52: registers the Phase 3 list kinds  # noqa: F401
    lists_p4,  # ruff: ignore[unused-import] - Epic 65: registers the Phase 4 list kinds (agent runs / Proposals)  # noqa: F401
    summaries,
)
from app.services.reporting.common import principal_of, tenant_tz
from app.services.reporting.export import ExportResult

ACTIVE_DAYS_RETENTION = 400


# ================================================================ dashboards
async def list_dashboards(ctx: RequestContext, db: AsyncSession) -> dict[str, Any]:
    return {"items": await dashboards.dashboards_for(ctx, db)}


async def get_tile(
    ctx: RequestContext,
    db: AsyncSession,
    dashboard: str,
    tile_key: str,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    business_unit_ids: list[uuid.UUID] | None = None,
    granularity: str | None = None,
    refresh: bool = False,
    legal_entity_ids: list[uuid.UUID] | None = None,
    as_of: date | None = None,
    agent_key: str | None = None,
) -> dict[str, Any]:
    tile = await dashboards.resolve_tile(ctx, db, dashboard, tile_key)
    principal = principal_of(ctx)
    resolved = await filters.resolve(
        db,
        principal,
        await tenant_tz(db),
        date_from,
        date_to,
        business_unit_ids,
        granularity,
        legal_entity_ids=legal_entity_ids,
        as_of=as_of,
        agent_key=agent_key,
    )
    return await dashboards.compute_tile(ctx, db, dashboard, tile, resolved, refresh=refresh)


# ================================================================ Epic 34: executive grid, reconciliation
async def executive_grid(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    legal_entity_id: uuid.UUID | None = None,
    month_from: date | None = None,
    month_to: date | None = None,
) -> dict[str, Any]:
    from app.core.errors import NotFound
    from app.core.permissions import can_view_compensation

    principal = principal_of(ctx)
    tz = await tenant_tz(db)
    first, last = filters.month_range(month_from, month_to, common.now().astimezone(tz).date())
    if legal_entity_id is not None:
        les = list(await filters.resolve_legal_entities(db, [legal_entity_id]))
    else:
        les = await summaries.legal_entity_ids(db)
    if not les:
        raise NotFound("No legal entity", code="not_available")
    comp = can_view_compensation(principal)
    return await summaries.exec_grid(ctx, db, les[0], first, last, compensation=comp)


async def reconciliation_issues(ctx: RequestContext, db: AsyncSession) -> dict[str, Any]:
    principal_of(ctx)
    return {"items": await summaries.open_issues(db)}


async def rebuild_summaries(db: AsyncSession, legal_entity_id: uuid.UUID, month: date) -> tuple[int, int]:
    return await summaries.rebuild(db, legal_entity_id, month)


async def verify_summaries(db: AsyncSession, months: list[date] | None = None) -> dict[str, int]:
    return await summaries.verify(db, months)


# ================================================================ report lists
async def list_report(
    ctx: RequestContext, db: AsyncSession, kind: str, list_filters: BaseModel, params: PageParams
) -> dict[str, Any]:
    return await lists.list_rows(ctx, db, kind, list_filters, params)


# ================================================================ exports
async def request_export(ctx: RequestContext, db: AsyncSession, body: ExportCreate) -> ExportResult:
    return await export.request_export(ctx, db, body)


async def list_exports(ctx: RequestContext, db: AsyncSession, params: PageParams) -> Page[ExportOut]:
    return await export.list_exports(ctx, db, params)


async def get_export(ctx: RequestContext, db: AsyncSession, export_id: uuid.UUID) -> ExportOut:
    return await export.get_export(ctx, db, export_id)


async def download_export(ctx: RequestContext, db: AsyncSession, export_id: uuid.UUID) -> uuid.UUID:
    return await export.download(ctx, db, export_id)


async def run_export(db: AsyncSession, export_id: uuid.UUID) -> str:
    return await export.run_export(db, export_id)


async def purge_expired_exports(db: AsyncSession, at: datetime | None = None) -> int:
    return await export.purge_expired(db, at)


# ================================================================ SM-3: active days
_seen: dict[tuple[uuid.UUID, uuid.UUID], date] = {}
_tz_cache: dict[uuid.UUID, tuple[ZoneInfo, float]] = {}
_seen_lock = threading.Lock()
_TZ_TTL = 3600.0


async def _claim_redis(key: str) -> bool | None:
    """``SET key NX EX 86400`` -- True when this process should write the row; None when Redis is down."""
    from app.core.settings import get_settings

    s = get_settings()
    if s.rate_limit_backend != "redis":
        return None
    try:
        from redis.asyncio import Redis

        client = Redis(
            host=s.redis_host,
            port=s.redis_port,
            db=2,
            password=s.redis_password or None,
            socket_connect_timeout=0.3,
            socket_timeout=0.3,
        )
        try:
            return bool(await client.set(key, "1", nx=True, ex=86400))
        finally:
            await client.aclose()
    except Exception as exc:
        logger.debug("Active-day claim fell back to the database ({})", type(exc).__name__)
        return None


async def touch_active_day(principal: Principal) -> None:
    """Record that ``principal`` (a tenant user) was active today (tenant timezone): at most once per user per
    day -- in-process memo, then Redis ``SET NX``, then ``INSERT ... ON CONFLICT DO NOTHING`` in its own short
    transaction. Never raises."""
    if principal.type != "user" or principal.tenant_id is None or principal.read_only:
        return
    tenant_id, user_id = principal.tenant_id, principal.id
    try:
        from app.core.database import tenant_session

        cached = _tz_cache.get(tenant_id)
        tz = cached[0] if cached and cached[1] > time.monotonic() else None
        today = common.now().astimezone(tz).date() if tz else None
        if today is not None and _seen.get((tenant_id, user_id)) == today:
            return
        async with tenant_session(tenant_id) as db:
            if tz is None:
                tz = await tenant_tz(db)
                _tz_cache[tenant_id] = (tz, time.monotonic() + _TZ_TTL)
                today = common.now().astimezone(tz).date()
                if _seen.get((tenant_id, user_id)) == today:
                    return
            assert today is not None
            claimed = await _claim_redis(f"uad:{tenant_id}:{user_id}:{today.isoformat()}")
            if claimed is not False:
                await db.execute(
                    pg_insert(UserActiveDay)
                    .values(tenant_id=tenant_id, user_id=user_id, day=today)
                    .on_conflict_do_nothing()
                )
        with _seen_lock:
            if len(_seen) > 100_000:
                _seen.clear()
            _seen[tenant_id, user_id] = today
    except Exception as exc:
        logger.warning("Could not record the active day ({})", type(exc).__name__)


async def purge_active_days(db: AsyncSession, today: date | None = None) -> int:
    today = today or common.now().date()
    result = await db.execute(
        delete(UserActiveDay).where(UserActiveDay.day < today - timedelta(days=ACTIVE_DAYS_RETENTION))
    )
    return int(result.rowcount or 0)


# ================================================================ wiring
_registered = False


def _on_export_queued(event: Any) -> None:
    from app.tasks import reporting as reporting_tasks
    from app.tasks.runtime import enqueue

    enqueue(reporting_tasks.run_export, str(event.tenant_id), str(event.export_id))


def register() -> None:
    """Idempotent wiring: the export-file access hook and the ExportQueued -> Celery handler."""
    global _registered  # process-wide wiring
    if _registered:
        return
    from app.core.events import subscribe
    from app.events.reporting import ExportQueued
    from app.services import files_service

    files_service.register_access_checker("export", export.file_access, plural="exports")
    subscribe(ExportQueued, _on_export_queued)
    _registered = True


register()

__all__ = [
    "ExportResult",
    "download_export",
    "executive_grid",
    "get_export",
    "get_tile",
    "list_dashboards",
    "list_exports",
    "list_report",
    "lists_p2",
    "purge_active_days",
    "purge_expired_exports",
    "rebuild_summaries",
    "reconciliation_issues",
    "register",
    "request_export",
    "run_export",
    "touch_active_day",
    "verify_summaries",
]
