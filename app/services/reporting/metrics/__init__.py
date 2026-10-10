"""Tile metric functions (Epic 11, A3): one async function per tile, grouped by dashboard.

Every function takes a ``TileContext`` and returns a ``TileData`` dict (``value``, ``unit``, ``n``,
``rows``, ``series``, ``link``, ``row_links``). Aggregates run in SQL (``count(*) FILTER``,
``percentile_cont``, ``date_trunc`` + ``generate_series``) inside the tenant session (RLS).

Count KPIs are computed **from the list query of their drill-down link** (``count_link``), so clicking a
KPI always lists exactly the counted rows.
"""

import dataclasses
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.permissions import Principal, Scope
from app.services.reporting import lists
from app.services.reporting.filters import REPORT_READ, DashboardFilters

TileData = dict[str, Any]
Query = dict[str, str | list[str]]

LIST_PATHS: dict[str, str] = {
    "candidates": "/candidates",
    "demands": "/demands",
    "applications": "/reports/applications",
    "submissions": "/reports/submissions",
    "interviews": "/reports/interviews",
    "offers": "/reports/offers",
}


@dataclasses.dataclass(frozen=True, slots=True)
class TileContext:
    ctx: RequestContext
    db: AsyncSession
    principal: Principal
    filters: DashboardFilters
    avail: frozenset[str]
    now: datetime
    dashboard: str

    @property
    def me(self) -> uuid.UUID:
        return self.principal.id

    @property
    def client_owner(self) -> uuid.UUID | None:
        """Account dashboard with ``own`` / ``team`` report scope: only clients the caller owns."""
        if self.dashboard != "account":
            return None
        scope = self.principal.scope_of(REPORT_READ)
        return self.principal.id if scope is None or not scope.covers(Scope.BU) else None

    def scope_query(self) -> Query:
        """Query parameters narrowing a drill-down to this dashboard's scope (BUs, owned clients)."""
        query: Query = dict(self.filters.bu_query())
        if self.client_owner is not None:
            query["client_owner"] = "me"
        return query

    def range_query(self) -> Query:
        return dict(self.filters.range_query())


def link(path: str, query: Mapping[str, Any] | None = None) -> dict[str, Any]:
    clean: dict[str, str | list[str]] = {}
    for key, value in (query or {}).items():
        if value is None or value == [] or value == "":
            continue
        if isinstance(value, list | tuple):
            clean[key] = [str(v) for v in value]
        elif isinstance(value, bool):
            clean[key] = "true" if value else "false"
        else:
            clean[key] = str(value)
    return {"path": path, "query": clean}


async def list_query(t: TileContext, kind: str, query: Mapping[str, Any]) -> lists.ListQuery:
    """Build the drill-down list query for ``kind`` with the (link) ``query`` parameters."""
    parsed = lists.parse_filters(kind, link("", query)["query"])
    return await lists.build(t.ctx, t.db, kind, parsed)


async def count_link(t: TileContext, kind: str, query: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Count the rows of the drill-down list (``kind`` + ``query``) and return it with its link."""
    lq = await list_query(t, kind, query)
    total = int((await t.db.execute(lq.count())).scalar_one())
    return total, link(LIST_PATHS[kind], query)


def ids_of(lq: lists.ListQuery) -> Any:
    """``SELECT id`` of a list query (for ``IN``), without its ORDER BY."""
    return lq.base.order_by(None)


def pct(part: int | float | None, whole: int | float | None) -> float | None:
    if not whole:
        return None
    return round(100.0 * float(part or 0) / float(whole), 1)


def rounded(value: Any, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


def epoch(expr: Any) -> Any:
    return func.extract("epoch", expr)


__all__ = [
    "LIST_PATHS",
    "Query",
    "TileContext",
    "TileData",
    "count_link",
    "epoch",
    "ids_of",
    "link",
    "list_query",
    "pct",
    "rounded",
    "select",
]
