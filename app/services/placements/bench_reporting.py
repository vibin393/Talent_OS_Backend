"""Bench reporting (Epic 24, FR-127): Delivery Manager dashboard figures, computed live (AD-34).

Money is per currency, never summed across currencies; each entry-month product is quantized then summed
(``bench_cost.cost_by_month``). Without ``compensation:view``
money figures are ``None`` and ``masked`` is true.
"""

import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.money import MONEY_CONTEXT, quantize_amount
from app.models.placements import BENCH_OPEN_STATUSES, BenchEntry, Placement
from app.schemas.bench import BenchSummaryOut, MoneyByCurrency, MonthCost, RollOffRow, WorkerBenchDays
from app.services.placements import bench, bench_cost

_RATE_Q = Decimal("0.0001")


def _months_back(as_of: date, months: int) -> date:
    y, m = as_of.year, as_of.month - (months - 1)
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


async def _tz(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    from app.services.directory_service import tenant_timezone

    try:
        return await tenant_timezone(db, tenant_id) or "Asia/Kolkata"
    except Exception:  # pragma: no cover
        return "Asia/Kolkata"


async def roll_offs(
    ctx: RequestContext,
    db: AsyncSession,
    within: int,
    *,
    as_of: date | None = None,
    client_id: uuid.UUID | None = None,
) -> list[RollOffRow]:
    from app.services.placements.common import names

    tid = bench._tenant(ctx, db)
    today = as_of or await bench.tenant_today(db, tid)
    ends = func.coalesce(Placement.end_date, Placement.planned_end_date)
    stmt = select(Placement, ends).where(
        Placement.status == "active", ends >= today, ends <= today + timedelta(days=within)
    )
    if client_id is not None:
        stmt = stmt.where(Placement.client_id == client_id)
    rows = (await db.execute(stmt.order_by(ends, Placement.id))).all()
    cands, clients = await names(
        db, candidate_ids={p.candidate_id for p, _ in rows}, client_ids={p.client_id for p, _ in rows}
    )
    return [
        RollOffRow(
            placement_id=p.id,
            placement_human_id=p.human_id,
            candidate_id=p.candidate_id,
            candidate_name=cands.get(p.candidate_id),
            client_id=p.client_id,
            client_name=clients.get(p.client_id),
            role_title=p.role_title,
            end_date=end,
            days_left=(end - today).days,
        )
        for p, end in rows
    ]


async def summary(
    ctx: RequestContext,
    db: AsyncSession,
    as_of: date | None = None,
    *,
    months: int = 6,
    legal_entity_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
) -> BenchSummaryOut:
    from app.services.placements.common import names

    tid = bench._tenant(ctx, db)
    settings = await bench.settings_row(db, tid)
    basis = settings.bench_day_basis
    tz = await _tz(db, tid)
    as_of = as_of or await bench.tenant_today(db, tid)
    window_start = _months_back(as_of, months)
    show = bench._show_comp(ctx)

    stmt = select(BenchEntry).where(BenchEntry.available_from <= as_of)
    if legal_entity_id is not None:
        stmt = stmt.where(BenchEntry.legal_entity_id == legal_entity_id)
    if client_id is not None:
        stmt = stmt.where(
            BenchEntry.source_placement_id.in_(select(Placement.id).where(Placement.client_id == client_id))
        )
    entries = list((await db.execute(stmt)).scalars())

    open_entries = [e for e in entries if e.status in BENCH_OPEN_STATUSES]
    cands, _ = await names(db, candidate_ids={e.candidate_id for e in open_entries})
    by_worker: list[WorkerBenchDays] = []
    total_days = 0
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for e in open_entries:
        days = bench_cost.bench_days(e.available_from, as_of, basis)
        total_days += days
        by_worker.append(
            WorkerBenchDays(
                bench_entry_id=e.id,
                candidate_id=e.candidate_id,
                candidate_name=cands.get(e.candidate_id),
                status=e.status,
                bench_days=days,
            )
        )
        to_date = bench_cost.cost_to_date(e.cost_per_day, days, e.cost_currency)
        if to_date is not None and e.cost_currency:
            totals[e.cost_currency] = MONEY_CONTEXT.add(totals[e.cost_currency], to_date)
    by_worker.sort(key=lambda w: (-w.bench_days, str(w.candidate_id)))

    monthly: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal(0))
    for e in entries:
        if e.cost_per_day is None or not e.cost_currency:
            continue
        until = min(bench.until_for(e, as_of, tz), as_of)
        start = max(e.available_from, window_start)
        if start > until:
            continue
        for month, amount in bench_cost.cost_by_month(
            e.cost_per_day, e.cost_currency, start, until, basis
        ).items():
            monthly[month, e.cost_currency] = MONEY_CONTEXT.add(monthly[month, e.cost_currency], amount)

    closed_in_window = [
        e
        for e in entries
        if e.closed_at is not None and window_start <= bench.until_for(e, as_of, tz) <= as_of
    ]
    redeployed = sum(1 for e in closed_in_window if e.status == "redeployed")
    released = sum(1 for e in closed_in_window if e.status == "released")
    rate = (
        MONEY_CONTEXT.divide(Decimal(redeployed), Decimal(redeployed + released)).quantize(_RATE_Q)
        if redeployed + released
        else None
    )
    avg = (
        MONEY_CONTEXT.divide(Decimal(total_days), Decimal(len(open_entries))).quantize(Decimal("0.1"))
        if open_entries
        else None
    )
    r30 = await roll_offs(ctx, db, 30, as_of=as_of, client_id=client_id)
    r60 = await roll_offs(ctx, db, 60, as_of=as_of, client_id=client_id)
    return BenchSummaryOut(
        as_of=as_of,
        count_on_bench=len(open_entries),
        avg_bench_days=avg,
        bench_days_by_worker=by_worker,
        total_cost_to_date=[
            MoneyByCurrency(currency=c, amount=quantize_amount(v, c) if show else None)
            for c, v in sorted(totals.items())
        ],
        cost_by_month=[
            MonthCost(month=m, currency=c, amount=v if show else None)
            for (m, c), v in sorted(monthly.items())
        ],
        upcoming_roll_offs_30=len(r30),
        upcoming_roll_offs_60=len(r60),
        redeployment_rate=rate,
        redeployed=redeployed,
        released=released,
        masked=not show,
        filters={
            "count_on_bench": {"status": ["on_bench", "proposed"]},
            "avg_bench_days": {"status": ["on_bench", "proposed"]},
            "total_cost_to_date": {"status": ["on_bench", "proposed"]},
            "redeployment_rate": {"status": ["redeployed", "released"]},
            "upcoming_roll_offs_30": {"within": 30},
            "upcoming_roll_offs_60": {"within": 60},
        },
    )
