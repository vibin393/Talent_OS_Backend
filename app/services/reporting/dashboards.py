"""Dashboards and the tile registry (Epic 11, A3, FR-60).

* ``DASHBOARDS`` -- recruiter / manager / account, each an ordered list of ``TileDef``.
* ``dashboards_for(ctx, db)`` -- the dashboards the caller may open, default first (role mapping:
  ``recruiter`` -> recruiter; ``recruitment_manager`` / ``delivery_manager`` -> manager; ``account_manager``
  -> account; ``tenant_admin`` -> all three, manager first). Callers with ``report:read`` but none of those
  roles get manager (``bu`` / ``tenant`` report scope) or recruiter.
* ``compute_tile(...)`` -- one tile, independently: optional-table / permission check (``unavailable``),
  Redis cache ``dash:{tenant}:{user}:{tile}:{sha1(filters)}`` (``DASHBOARD_CACHE_SECONDS``, bypassed by
  ``refresh``), and ``SET LOCAL statement_timeout`` inside a savepoint (a timeout -> ``timeout``).
"""

import asyncio
import dataclasses
import json
import weakref
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import DomainError, NotFound
from app.core.permissions import TENANT_ADMIN, Principal, Scope
from app.core.settings import get_settings
from app.services.reporting.common import available, has_interviews, has_offers, now, principal_of, role_keys
from app.services.reporting.filters import REPORT_READ, DashboardFilters
from app.services.reporting.metrics import TileContext, TileData
from app.services.reporting.metrics import account as account_tiles
from app.services.reporting.metrics import manager as manager_tiles
from app.services.reporting.metrics import recruiter as recruiter_tiles
from app.services.reporting.summaries import NotAvailable

TileFn = Callable[[TileContext], Awaitable[TileData]]
Requirement = Callable[[frozenset[str]], bool]


@dataclasses.dataclass(frozen=True, slots=True)
class TileDef:
    key: str
    title: str
    kind: str  # kpi | table | series | breakdown | funnel
    size: str  # s | m | l
    description: str
    fn: TileFn
    requires: Requirement | None = None  # optional tables (Epics 7-8)
    permissions: tuple[str, ...] = ()  # read permissions of the lists behind the tile
    compensation: bool = False  # Epic 34: money detail masked without compensation:view
    estimate: bool = False  # Epic 34: WIP figures ("Approved, not yet invoiced")


@dataclasses.dataclass(frozen=True, slots=True)
class DashboardDef:
    key: str
    title: str
    tiles: tuple[TileDef, ...]

    def tile(self, key: str) -> TileDef | None:
        return next((t for t in self.tiles if t.key == key), None)


_R, _M, _A = recruiter_tiles, manager_tiles, account_tiles

DASHBOARDS: dict[str, DashboardDef] = {
    "recruiter": DashboardDef(
        "recruiter",
        "My work",
        (
            TileDef(
                "my_open_demands", "My open demands", "table", "l",
                "Open demands you are assigned to, worst SLA first: SLA status, days open and pipeline size.",
                _R.my_open_demands, permissions=("demand:read",),
            ),
            TileDef(
                "my_pipeline_by_stage", "My pipeline by stage", "breakdown", "m",
                "Active applications you own or that sit on your demands, by stage.",
                _R.my_pipeline_by_stage, permissions=("application:read",),
            ),
            TileDef(
                "tasks_due", "Tasks", "kpi", "s",
                "Your open tasks due today and overdue.",
                _R.tasks_due, permissions=("task:read",),
            ),
            TileDef(
                "interviews_today", "Interviews today", "table", "m",
                "Interviews today (your timezone) where you are a participant or own the application.",
                _R.interviews_today, requires=has_interviews, permissions=("interview:read",),
            ),
            TileDef(
                "my_week", "This week", "kpi", "m",
                "This ISO week: submissions you sent, interviews and offers on your applications.",
                _R.my_week, permissions=("submission:read",),
            ),
        ),
    ),
    "manager": DashboardDef(
        "manager",
        "Team performance",
        (
            TileDef(
                "open_demands", "Open demands", "kpi", "s",
                "Demands currently Open.", _M.open_demands, permissions=("demand:read",),
            ),
            TileDef(
                "sla_risk", "SLA risk", "kpi", "s",
                "Open demands with a running SLA clock At risk / Breached.",
                _M.sla_risk, permissions=("demand:read",),
            ),
            TileDef(
                "throughput", "Throughput", "series", "l",
                "Submissions sent, interviews scheduled, offers released and hires per period.",
                _M.throughput, permissions=("submission:read", "application:read"),
            ),
            TileDef(
                "time_to_first_submission", "Time to first submission", "kpi", "s",
                "SM-1: median hours from a demand opening to its first submission (demands opened in range).",
                _M.time_to_first_submission, permissions=("demand:read",),
            ),
            TileDef(
                "time_to_fill", "Time to fill", "kpi", "s",
                "Median days from opening to the hire that filled the last position "
                "(demands filled in range).",
                _M.time_to_fill, permissions=("demand:read",),
            ),
            TileDef(
                "recruiter_activity", "Recruiter activity", "table", "l",
                "Per recruiter in range: applications, screenings, submissions, interviews, "
                "notes/calls, emails.",
                _M.recruiter_activity, permissions=("submission:read",),
            ),
            TileDef(
                "stage_conversion", "Stage conversion", "funnel", "m",
                "Applications created in range that ever reached each stage, and % of the previous stage.",
                _M.stage_conversion, permissions=("application:read",),
            ),
            TileDef(
                "source_effectiveness", "Source effectiveness", "table", "m",
                "By application source: applications, submissions, interviews, hires and hire rate.",
                _M.source_effectiveness, permissions=("application:read",),
            ),
            TileDef(
                "db_sourced_submissions", "Database-sourced submissions", "kpi", "s",
                "SM-2: % of submissions sent in range whose candidate existed before the demand opened.",
                _M.db_sourced_submissions, permissions=("submission:read",),
            ),
            TileDef(
                "submission_to_interview", "Submission to interview", "kpi", "s",
                "SM-6: % of submissions sent in range whose application has an interview.",
                _M.submission_to_interview, requires=has_interviews, permissions=("submission:read",),
            ),
            TileDef(
                "active_recruiters", "Weekly active recruiters", "kpi", "s",
                "SM-3: % of recruiters active on at least 4 of the last 7 days.",
                _M.active_recruiters,
            ),
        ),
    ),
    "account": DashboardDef(
        "account",
        "Clients",
        (
            TileDef(
                "demands_by_client", "Demands by client", "table", "l",
                "Per client: open, on hold, filled in range, positions, hires, fill progress, submissions, "
                "interviews.",
                _A.demands_by_client, permissions=("demand:read",),
            ),
            TileDef(
                "awaiting_feedback", "Awaiting client feedback", "kpi", "s",
                "Sent submissions without client feedback for more than 3 business days.",
                _A.awaiting_feedback, permissions=("submission:read",),
            ),
            TileDef(
                "offers_pending", "Offers pending", "kpi", "s",
                "Released or viewed offers the candidate has not answered yet.",
                _A.offers_pending, requires=has_offers, permissions=("offer:read",),
            ),
        ),
    ),
}  # fmt: skip

ROLE_DASHBOARDS: dict[str, tuple[str, ...]] = {
    TENANT_ADMIN: ("manager", "recruiter", "account"),
    "recruitment_manager": ("manager",),
    "delivery_manager": ("manager",),
    "account_manager": ("account",),
    "recruiter": ("recruiter",),
}
ORDER = ("manager", "account", "recruiter")
# Epic 34: Phase 2 dashboards (defined in dashboards_p2, merged into DASHBOARDS below).
P2_KEYS = frozenset({"finance", "account_ops", "onboarding", "compliance", "delivery", "executive"})
# Epic 52: Phase 3 dashboards (defined in dashboards_p3, merged into DASHBOARDS below).
P3_KEYS = frozenset(
    {"marketing", "referrals", "campus", "mobility", "skills", "projects", "immigration", "predictions"}
)
# Epic 65: Phase 4 dashboards (defined in dashboards_p4, merged into DASHBOARDS below).
P4_KEYS = frozenset({"agents", "agent_outcomes"})


# ================================================================ listing
async def dashboard_keys(db: AsyncSession, principal: Principal) -> list[str]:
    from app.services.reporting.dashboards_p4 import p4_keys

    if not principal.has(REPORT_READ):  # Epic 65: ai:govern without report:read -> AI agents dashboard only
        return p4_keys(principal)
    keys = await role_keys(db, principal.id)
    wanted: list[str] = []
    if TENANT_ADMIN in keys:
        wanted.extend(ROLE_DASHBOARDS[TENANT_ADMIN])
    for key in ORDER:
        if key not in wanted and any(key in ROLE_DASHBOARDS.get(role, ()) for role in keys):
            wanted.append(key)
    from app.services.reporting.dashboards_p2 import p2_keys

    first, p2 = p2_keys(principal, keys, TENANT_ADMIN in keys)
    if not wanted and not p2:
        scope = principal.scope_of(REPORT_READ)
        wanted.append("manager" if scope is not None and scope.covers(Scope.BU) else "recruiter")
    # Epic 34 (FR-81): a Phase 2 default role's own dashboard first; other allowed P2 dashboards after P1's.
    # Epic 52 (FR-260): allowed Phase 3 dashboards last.
    from app.services.reporting.dashboards_p3 import p3_keys

    return list(dict.fromkeys([*first, *wanted, *p2, *p3_keys(principal), *p4_keys(principal)]))


def tile_available(tile: TileDef, principal: Principal, avail: frozenset[str]) -> bool:
    from app.services.reporting.filters import perm

    if tile.requires is not None and not tile.requires(avail):
        return False
    return all(perm(p) in principal.permissions for p in tile.permissions)


async def dashboards_for(ctx: RequestContext, db: AsyncSession) -> list[dict[str, Any]]:
    principal = principal_of(ctx)
    avail = await available(db)
    out: list[dict[str, Any]] = []
    for index, key in enumerate(await dashboard_keys(db, principal)):
        dash = DASHBOARDS[key]
        out.append(
            {
                "key": dash.key,
                "title": dash.title,
                "default": index == 0,
                "tiles": [
                    {
                        "key": t.key,
                        "title": t.title,
                        "kind": t.kind,
                        "size": t.size,
                        "description": t.description,
                        "available": tile_available(t, principal, avail),
                        "compensation": t.compensation,
                        "estimate": t.estimate,
                    }
                    for t in dash.tiles
                ],
            }
        )
    return out


async def resolve_tile(ctx: RequestContext, db: AsyncSession, dashboard: str, tile_key: str) -> TileDef:
    principal = principal_of(ctx)
    if dashboard not in DASHBOARDS or dashboard not in await dashboard_keys(db, principal):
        raise NotFound("Dashboard not found")
    tile = DASHBOARDS[dashboard].tile(tile_key)
    if tile is None:
        raise NotFound("Tile not found")
    return tile


# ================================================================ cache
class _Cache:
    """Best-effort Redis cache (one client per event loop); every failure is a miss."""

    def __init__(self) -> None:
        # keyed by the loop object (not id(): ids are reused after a loop is collected)
        self._clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any] = weakref.WeakKeyDictionary()

    def _client(self) -> Any:
        from redis.asyncio import Redis

        loop_key = asyncio.get_running_loop()
        if loop_key not in self._clients:
            s = get_settings()
            self._clients[loop_key] = Redis(
                host=s.redis_host,
                port=s.redis_port,
                db=3,
                password=s.redis_password or None,
                decode_responses=True,
                socket_connect_timeout=0.3,
                socket_timeout=0.3,
            )
        return self._clients[loop_key]

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self._client().get(key)
        except Exception as exc:
            logger.debug("Dashboard cache read failed ({})", type(exc).__name__)
            return None
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: dict[str, Any], ttl: int) -> None:
        try:
            await self._client().set(key, json.dumps(value, default=str), ex=ttl)
        except Exception as exc:
            logger.debug("Dashboard cache write failed ({})", type(exc).__name__)


cache = _Cache()


def cache_key(principal: Principal, tile: TileDef, dashboard: str, filters: DashboardFilters) -> str:
    return f"dash:{principal.tenant_id}:{principal.id}:{dashboard}.{tile.key}:{filters.cache_key()}"


# ================================================================ compute
def _is_timeout(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    return code == "57014" or "statement timeout" in str(exc).lower()


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def _base(tile: TileDef, filters: DashboardFilters, status: str) -> dict[str, Any]:
    return {
        "key": tile.key,
        "kind": tile.kind,
        "title": tile.title,
        "status": status,
        "computed_at": now().isoformat(),
        "granularity": filters.granularity if tile.kind == "series" else None,
        "date_from": filters.from_date.isoformat(),
        "date_to": filters.to_date.isoformat(),
    }


async def compute_tile(
    ctx: RequestContext,
    db: AsyncSession,
    dashboard: str,
    tile: TileDef,
    filters: DashboardFilters,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    principal = principal_of(ctx)
    avail = await available(db)
    if not tile_available(tile, principal, avail):
        return _base(tile, filters, "unavailable")
    ttl = get_settings().dashboard_cache_seconds
    key = cache_key(principal, tile, dashboard, filters)
    if ttl > 0 and not refresh:
        cached = await cache.get(key)
        if cached is not None:
            return cached
    t = TileContext(
        ctx=ctx, db=db, principal=principal, filters=filters, avail=avail, now=now(), dashboard=dashboard
    )
    # Epic 52 / 65: Phase 3 and Phase 4 tiles use the P2 timeout / as-of.
    p2 = dashboard in P2_KEYS or dashboard in P3_KEYS or dashboard in P4_KEYS
    timeout_ms = int(
        get_settings().dashboard_p2_statement_timeout_ms if p2 else get_settings().dashboard_tile_timeout_ms
    )
    try:
        async with db.begin_nested():
            await db.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
            data = await tile.fn(t)
            await db.execute(text("SET LOCAL statement_timeout = 0"))
    except DBAPIError as exc:
        if not _is_timeout(exc):
            raise
        logger.warning("Dashboard tile {}.{} timed out after {} ms", dashboard, tile.key, timeout_ms)
        return _base(tile, filters, "timeout")
    except (NotAvailable, NotImplementedError) as exc:
        # Epic 34: an owner function / table of an epic that has not merged yet -> not available.
        logger.debug("Dashboard tile {}.{} not available: {}", dashboard, tile.key, exc)
        return _base(tile, filters, "unavailable")
    except DomainError as exc:
        # E.g. a list behind the tile the caller may not read (403) or that does not exist here.
        logger.debug("Dashboard tile {}.{} unavailable: {}", dashboard, tile.key, exc.code)
        return _base(tile, filters, "unavailable")
    out = {**_base(tile, filters, "ok"), **_jsonable(data)}
    if p2:
        out.setdefault("estimate", tile.estimate)
        out["as_of"] = filters.as_of.isoformat()
    if ttl > 0:
        await cache.set(key, out, ttl)
    return out


# Epic 34: merge the Phase 2 dashboards into the registry (module defined after this one's classes).
from app.services.reporting.dashboards_p2 import P2_DASHBOARDS

DASHBOARDS.update(P2_DASHBOARDS)

# Epic 52: merge the Phase 3 dashboards.
from app.services.reporting.dashboards_p3 import P3_DASHBOARDS

DASHBOARDS.update(P3_DASHBOARDS)

# Epic 65: merge the Phase 4 dashboards.
from app.services.reporting.dashboards_p4 import P4_DASHBOARDS

DASHBOARDS.update(P4_DASHBOARDS)
