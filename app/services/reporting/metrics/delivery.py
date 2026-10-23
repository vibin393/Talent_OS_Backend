# ruff: file-ignore[line-too-long, lowercase-imported-as-non-lowercase]
"""Delivery dashboard tiles (Epic 34, A3): bench, vendor performance, early terminations, roll-offs."""

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from app.services.reporting import facts
from app.services.reporting import p2tables as T
from app.services.reporting.metrics import TileContext, TileData
from app.services.reporting.metrics.p2common import P, comp, le_cond, le_link, need, scoped_placements

OPEN_BENCH = ("on_bench", "proposed")
EARLY_DAYS = 30


async def bench_summary(t: TileContext) -> TileData:
    need(t, "bench_entries")
    b = T.bench_entries.c
    today = t.now.date()
    days = today - b.available_from
    stmt = select(
        func.count(), func.coalesce(func.sum(days), 0), func.coalesce(func.sum(days * b.cost_per_day), 0)
    ).where(b.status.in_(OPEN_BENCH), b.available_from <= today, *le_cond(t, b.legal_entity_id))
    n, bench_days, cost = (await t.db.execute(stmt)).one()
    out: dict[str, Any] = {
        "value": int(n),
        "rows": [{"key": "bench_days", "label": "Bench days", "value": int(bench_days)}],
        "unit": "on bench",
        "link": le_link(t, "/bench"),
        "masked": not comp(t),
    }
    if comp(t):
        out["money"] = facts.money(Decimal(cost))
    return out


async def vendor_performance_summary(t: TileContext) -> TileData:
    need(t, "vendors", "vendor_submissions")
    v, vs = T.vendors.c, T.vendor_submissions.c
    f = t.filters
    accepted = func.count().filter(
        vs.status.in_(("accepted", "shortlisted", "submitted_to_client", "placed"))
    )
    rows = (
        await t.db.execute(
            select(v.id, v.legal_name, func.count(), accepted)
            .select_from(T.vendor_submissions.join(T.vendors, v.id == vs.vendor_id))
            .where(vs.created_at >= f.start, vs.created_at < f.end)
            .group_by(v.id, v.legal_name)
        )
    ).all()
    breaches: dict[Any, int] = {}
    if "sla_clocks" in t.avail:
        s = T.sla_clocks.c
        breaches = dict(
            (
                await t.db.execute(
                    select(s.org_vendor_id, func.count())
                    .where(
                        s.org_vendor_id.is_not(None),
                        s.status == "breached",
                        s.started_at >= f.start,
                        s.started_at < f.end,
                    )
                    .group_by(s.org_vendor_id)
                )
            )
            .tuples()
            .all()
        )
    early: dict[Any, int] = {}
    if "placements" in t.avail:
        early = dict(
            (
                await t.db.execute(
                    select(P.vendor_id, func.count())
                    .where(
                        P.vendor_id.is_not(None), P.end_date.is_not(None), P.status != "dropped",
                        P.end_date <= P.actual_start_date + EARLY_DAYS,
                        P.end_date >= f.from_date, P.end_date <= f.to_date,
                    )
                    .group_by(P.vendor_id)
                )
            ).tuples().all()
        )  # fmt: skip
    scored = []
    for vid, name, total, acc in rows:
        rate = None if not total else round(100.0 * int(acc) / int(total), 1)
        scored.append({"vendor": name, "submissions": int(total), "acceptance_rate": rate,
                       "sla_breaches": int(breaches.get(vid, 0)), "early_terminations": int(early.get(vid, 0))})  # fmt: skip
    scored.sort(key=lambda r: (r["acceptance_rate"] is None, -(r["acceptance_rate"] or 0)))
    picked = scored[:5] + [r for r in scored[-5:] if r not in scored[:5]]
    return {"rows": picked, "link": le_link(t, "/vendors")}


async def early_terminations(t: TileContext) -> TileData:
    need(t, "placements")
    f = t.filters
    ids = scoped_placements(
        t, P.status != "dropped", P.end_date >= f.from_date, P.end_date <= f.to_date,
        P.end_date <= P.actual_start_date + EARLY_DAYS,
    )  # fmt: skip
    from app.services.reporting.common import users

    rows = (  # type: ignore[var-annotated]
        await t.db.execute(
            select(
                func.coalesce(
                    select(T.vendors.c.legal_name).where(T.vendors.c.id == P.vendor_id).scalar_subquery()
                    if "vendors" in t.avail
                    else None,
                    select(users.c.name).where(users.c.id == P.recruiter_user_id).scalar_subquery(),
                ).label("source"),
                func.count(),
            )
            .where(P.id.in_(ids))
            .group_by("source")
            .order_by(func.count().desc())
        )
    ).all()
    return {
        "value": sum(int(r[1]) for r in rows),
        "rows": [{"source": r[0] or "Unknown", "early_terminations": int(r[1])} for r in rows],
    }


async def roll_offs_30_60(t: TileContext) -> TileData:
    need(t, "placements")
    today = t.now.date()
    end = P.planned_end_date
    n30, n60 = (
        await t.db.execute(
            select(func.count().filter(end <= today + timedelta(days=30)), func.count()).where(
                P.id.in_(
                    scoped_placements(
                        t,
                        P.status.in_(("active", "offboarding")),
                        end >= today,
                        end <= today + timedelta(days=60),
                    )
                )
            )
        )
    ).one()
    return {
        "rows": [
            {"key": "30", "label": "Next 30 days", "value": int(n30)},
            {"key": "60", "label": "Next 60 days", "value": int(n60)},
        ],
        "row_links": [
            le_link(t, "/placements", ending_within_days="30"),
            le_link(t, "/placements", ending_within_days="60"),
        ],
    }
