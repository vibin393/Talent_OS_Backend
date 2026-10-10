# ruff: noqa: E501
"""Usage reads (Epic 66; dashboards are Epic 79, partner views Epic 74). Source: the ``usage_counters`` daily rollup
(values reach it within one ``metering.flush`` period)."""

from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import platform_session, tenant_session
from app.models.identity import Tenant
from app.models.metering import UsageCounter
from app.schemas.metering import (
    PlatformUsageOut,
    PlatformUsageQuery,
    PlatformUsageRow,
    UnitTotal,
    UsageOut,
    UsageQuery,
    UsageRow,
)

if TYPE_CHECKING:
    import uuid

_GROUP_COLUMNS = {
    "day": UsageCounter.day,
    "unit": UsageCounter.unit,
    "installation": UsageCounter.installation_id,
    "api_key": UsageCounter.api_key_id,
    "route_group": UsageCounter.route_group,
}
_ROW_FIELDS = {
    "day": "day",
    "unit": "unit",
    "installation": "installation_id",
    "api_key": "api_key_id",
    "route_group": "route_group",
}


def _sums() -> list[Any]:
    return [
        func.coalesce(func.sum(UsageCounter.count), 0).label("count"),
        func.coalesce(func.sum(UsageCounter.error_count), 0).label("error_count"),
        func.coalesce(func.sum(UsageCounter.quantity), 0).label("quantity"),
    ]


async def usage(ctx: Any, db: AsyncSession, q: UsageQuery) -> UsageOut:  # noqa: ARG001 - ctx kept for the contract
    """Return the tenant's usage (``db`` is its tenant session, RLS-scoped)."""
    groups = list(dict.fromkeys(q.group_by))
    cols = [_GROUP_COLUMNS[g].label(_ROW_FIELDS[g]) for g in groups]
    where = [UsageCounter.day >= q.date_from, UsageCounter.day <= q.date_to]
    if q.units:
        where.append(UsageCounter.unit.in_(q.units))
    if q.installation_id is not None:
        where.append(UsageCounter.installation_id == q.installation_id)
    if q.api_key_id is not None:
        where.append(UsageCounter.api_key_id == q.api_key_id)
    stmt = select(*cols, *_sums()).where(*where).group_by(*[_GROUP_COLUMNS[g] for g in groups])
    stmt = stmt.order_by(*[_GROUP_COLUMNS[g] for g in groups])
    rows = [UsageRow(**dict(r)) for r in (await db.execute(stmt)).mappings()]
    total_stmt = (
        select(UsageCounter.unit, *_sums())
        .where(*where)
        .group_by(UsageCounter.unit)
        .order_by(UsageCounter.unit)
    )
    totals = [UnitTotal(**dict(r)) for r in (await db.execute(total_stmt)).mappings()]
    return UsageOut(date_from=q.date_from, date_to=q.date_to, group_by=groups, rows=rows, totals=totals)


async def platform_usage(q: PlatformUsageQuery) -> PlatformUsageOut:
    """Usage by App across tenants (operator). ``usage_counters`` is RLS-protected, so each tenant is read in its own
    tenant session and summed here; no tenant identity leaves this function (FR-385)."""
    async with platform_session() as pdb:
        stmt = select(Tenant.id)
        if not q.include_sandbox:
            stmt = stmt.where(Tenant.mode != "sandbox")
        tenant_ids: list[uuid.UUID] = list((await pdb.execute(stmt)).scalars())
    where = [UsageCounter.day >= q.date_from, UsageCounter.day <= q.date_to]
    if q.units:
        where.append(UsageCounter.unit.in_(q.units))
    if q.app_id is not None:
        where.append(UsageCounter.app_id == q.app_id)
    agg: dict[tuple[uuid.UUID | None, str], list[Any]] = defaultdict(lambda: [0, 0, Decimal(0), 0])
    stmt = (
        select(UsageCounter.app_id, UsageCounter.unit, *_sums())
        .where(*where)
        .group_by(UsageCounter.app_id, UsageCounter.unit)
    )
    for tid in tenant_ids:
        async with tenant_session(tid) as db:
            for r in (await db.execute(stmt)).mappings():
                row = agg[r["app_id"], r["unit"]]
                row[0] += int(r["count"])
                row[1] += int(r["error_count"])
                row[2] += Decimal(r["quantity"])
                row[3] += 1
    rows = [
        PlatformUsageRow(app_id=app_id, unit=unit, count=c, error_count=e, quantity=qty, tenant_count=n)
        for (app_id, unit), (c, e, qty, n) in sorted(
            agg.items(), key=lambda kv: (str(kv[0][0] or ""), kv[0][1])
        )
    ]
    return PlatformUsageOut(date_from=q.date_from, date_to=q.date_to, rows=rows)


__all__ = ["platform_usage", "usage"]
