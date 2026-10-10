"""Sales dashboard (FR-87, A7; story 2.5). SQL aggregates only (no ORM row loads); every query is narrowed to
the caller's data scope (``opportunity:read`` / ``lead:read`` on ``owner_id``) and the optional owner / team
filter. Money is grouped by currency (no FX). Each tile carries a ``link`` (frontend route + query) whose list
shows the same rows.
"""

import calendar
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import Numeric, case, cast, extract, func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.permissions import check
from app.models.crm import OPEN_STAGES, Lead, Opportunity
from app.models.identity import TeamMember
from app.schemas.crm import (
    CountTile,
    CountTotalsTile,
    DashboardOut,
    LeadsBySourceTile,
    MonthAmount,
    PipelineTile,
    SourceRow,
    StageAmount,
    WinRateTile,
)
from app.services import entity_refs, tasks_service
from app.services.crm.common import LEAD_READ, OPP_READ, now, principal_of, scoped, tenant_of, totals
from app.services.crm.opportunities import _zero_minor

MONTHS_AHEAD = 5


def _link(path: str, params: list[tuple[str, Any]]) -> str:
    query = urlencode([(k, str(v)) for k, v in params if v is not None])
    return f"{path}?{query}" if query else path


def _wtd(table: Any) -> Any:
    zero = _zero_minor() or ("---",)
    return func.round(
        cast(table.c.estimated_revenue * table.c.probability_pct / 100, Numeric),
        case((table.c.currency.in_(zero), 0), else_=2),
    )


def _add_months(d: date, n: int) -> date:
    month = d.month - 1 + n
    year = d.year + month // 12
    return date(year, month % 12 + 1, 1)


async def dashboard(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    owner_ids: list[uuid.UUID] | None = None,
    team_id: uuid.UUID | None = None,
) -> DashboardOut:
    principal = principal_of(ctx, OPP_READ)
    check(principal, LEAD_READ)
    tz = (await entity_refs.tenant(db, tenant_of(db))).tz
    today = now().astimezone(tz).date()
    to_date = to_date or today
    from_date = from_date or (to_date - timedelta(days=89))
    start = datetime.combine(from_date, time.min, tzinfo=tz)
    end = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=tz)

    owners: list[uuid.UUID] | None = list(owner_ids) if owner_ids else None
    if team_id is not None:
        members = list(
            (await db.execute(select(TeamMember.user_id).where(TeamMember.team_id == team_id))).scalars()
        )
        owners = [o for o in owners if o in members] if owners is not None else members
    owner_params = [("owner_id", o) for o in owners] if owners is not None else []

    def opps() -> Any:
        stmt = scoped(select(Opportunity), principal, OPP_READ, Opportunity.owner_id).where(
            Opportunity.archived_at.is_(None)
        )
        return stmt.where(Opportunity.owner_id.in_(owners)) if owners is not None else stmt

    def leads_q() -> Any:
        stmt = scoped(select(Lead), principal, LEAD_READ, Lead.owner_id).where(Lead.archived_at.is_(None))
        return stmt.where(Lead.owner_id.in_(owners)) if owners is not None else stmt

    open_stage_params = [("stage", s) for s in OPEN_STAGES]

    # ---- open / weighted pipeline by stage
    sub = opps().where(Opportunity.stage.in_(OPEN_STAGES)).subquery()
    rows = await db.execute(
        select(
            sub.c.stage, sub.c.currency, func.count(), func.sum(sub.c.estimated_revenue), func.sum(_wtd(sub))
        ).group_by(sub.c.stage, sub.c.currency)
    )
    by_stage: dict[str, dict[str, Any]] = {s: {"count": 0, "rows": []} for s in OPEN_STAGES}
    for stage, currency, count, est, wtd in rows.tuples():
        by_stage[stage]["count"] += int(count)
        by_stage[stage]["rows"].append((currency, est or Decimal(0), wtd or Decimal(0)))

    # ---- by expected-close month (current + next 5) and overdue close dates
    first = today.replace(day=1)
    horizon = _add_months(first, MONTHS_AHEAD + 1)
    month_key = case(
        (sub.c.expected_close_date < today, "overdue"),
        else_=func.to_char(sub.c.expected_close_date, "YYYY-MM"),
    )
    rows = await db.execute(
        select(
            month_key, sub.c.currency, func.count(), func.sum(sub.c.estimated_revenue), func.sum(_wtd(sub))
        )
        .where(sub.c.expected_close_date.is_not(None), sub.c.expected_close_date < horizon)
        .group_by(month_key, sub.c.currency)
    )
    months: dict[str, dict[str, Any]] = {"overdue": {"count": 0, "rows": []}}
    for i in range(MONTHS_AHEAD + 1):
        months[_add_months(first, i).strftime("%Y-%m")] = {"count": 0, "rows": []}
    for key, currency, count, est, wtd in rows.tuples():
        bucket = months.setdefault(str(key), {"count": 0, "rows": []})
        bucket["count"] += int(count)
        bucket["rows"].append((currency, est or Decimal(0), wtd or Decimal(0)))

    pipeline = PipelineTile(
        link=_link("/opportunities", [("view", "board"), *owner_params]),
        by_stage=[
            StageAmount(stage=s, count=v["count"], totals=totals(v["rows"])) for s, v in by_stage.items()
        ],
        by_close_month=[
            MonthAmount(month=k, count=v["count"], totals=totals(v["rows"])) for k, v in months.items()
        ],
    )

    # ---- closing this month (tenant timezone month)
    month_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    row = (
        await db.execute(
            select(sub.c.currency, func.count(), func.sum(sub.c.estimated_revenue), func.sum(_wtd(sub)))
            .where(sub.c.expected_close_date >= first, sub.c.expected_close_date <= month_end)
            .group_by(sub.c.currency)
        )
    ).tuples()
    closing_rows = [(c, e or Decimal(0), w or Decimal(0), int(n)) for c, n, e, w in row]
    closing = CountTotalsTile(
        link=_link(
            "/opportunities",
            [
                ("view", "list"),
                *open_stage_params,
                ("close_from", first),
                ("close_to", month_end),
                *owner_params,
            ],
        ),
        count=sum(r[3] for r in closing_rows),
        totals=totals((c, e, w) for c, e, w, _ in closing_rows),
    )

    # ---- win rate and average sales cycle (won_at / lost_at in range)
    all_sub = opps().subquery()
    won_in = (all_sub.c.stage == "won") & (all_sub.c.won_at >= start) & (all_sub.c.won_at < end)
    lost_in = (all_sub.c.stage == "lost") & (all_sub.c.lost_at >= start) & (all_sub.c.lost_at < end)
    won, lost, avg_seconds = (
        await db.execute(
            select(
                func.count().filter(won_in),
                func.count().filter(lost_in),
                func.avg(extract("epoch", all_sub.c.won_at - all_sub.c.created_at)).filter(won_in),
            )
        )
    ).one()
    won, lost = int(won or 0), int(lost or 0)
    rate = None if won + lost == 0 else (Decimal(won * 100) / Decimal(won + lost)).quantize(Decimal("0.1"))
    cycle = (
        None if avg_seconds is None else (Decimal(str(avg_seconds)) / Decimal(86400)).quantize(Decimal("0.1"))
    )
    win_rate = WinRateTile(
        link=_link("/opportunities", [("view", "list"), ("stage", "won"), ("stage", "lost"), *owner_params]),
        won=won,
        lost=lost,
        win_rate_pct=None if rate is None else format(rate, "f"),
        avg_sales_cycle_days=None if cycle is None else format(cycle, "f"),
    )

    # ---- leads by source (created in range) and conversion rate per source
    lsub = leads_q().where(Lead.created_at >= start, Lead.created_at < end).subquery()
    src = func.coalesce(lsub.c.source_code, "unknown")
    rows = await db.execute(
        select(src, func.count(), func.count().filter(lsub.c.status == "converted"))
        .group_by(src)
        .order_by(func.count().desc())
    )
    source_rows = [
        SourceRow(
            source_code=code,
            created=int(created),
            converted=int(converted),
            conversion_rate_pct=None
            if not created
            else format(
                (Decimal(int(converted) * 100) / Decimal(int(created))).quantize(Decimal("0.1")), "f"
            ),
        )
        for code, created, converted in rows.tuples()
    ]
    leads_tile = LeadsBySourceTile(
        link=_link("/leads", [("created_from", from_date), ("created_to", to_date), *owner_params]),
        rows=source_rows,
    )

    # ---- overdue tasks on in-scope leads / opportunities
    in_scope = union_all(
        scoped(select(Lead.id), principal, LEAD_READ, Lead.owner_id).where(
            *([Lead.owner_id.in_(owners)] if owners is not None else [])
        ),
        scoped(select(Opportunity.id), principal, OPP_READ, Opportunity.owner_id).where(
            *([Opportunity.owner_id.in_(owners)] if owners is not None else [])
        ),
    ).subquery()
    overdue = await tasks_service.count_overdue_for_entities(
        ctx, db, ["lead", "opportunity"], select(in_scope.c.id)
    )
    overdue_tile = CountTile(
        link=_link("/tasks", [("view", "overdue"), ("entity_type", "lead,opportunity")]), count=overdue
    )

    return DashboardOut(
        from_date=from_date,
        to_date=to_date,
        pipeline=pipeline,
        closing_this_month=closing,
        win_rate=win_rate,
        leads_by_source=leads_tile,
        overdue_tasks=overdue_tile,
    )
