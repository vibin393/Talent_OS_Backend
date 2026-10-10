# ruff: noqa: N812
"""Onboarding dashboard tiles (Epic 34, A3)."""

from datetime import timedelta

from sqlalchemy import case, func, select

from app.services.reporting import p2tables as T
from app.services.reporting.metrics import TileContext, TileData
from app.services.reporting.metrics.p2common import P, le_link, need, scoped_placements

PREJOIN = ("pre_joining", "joining_confirmed")


async def prejoining_by_days_to_start(t: TileContext) -> TileData:
    need(t, "placements")
    today = t.now.date()
    days = P.planned_start_date - today
    bucket = case((days <= 3, "0-3"), (days <= 7, "4-7"), (days <= 14, "8-14"), else_="15+")
    rows = dict(
        (
            await t.db.execute(
                select(bucket, func.count())
                .where(P.id.in_(scoped_placements(t, P.status.in_(PREJOIN))))
                .group_by(bucket)
            )
        )
        .tuples()
        .all()
    )
    keys = ("0-3", "4-7", "8-14", "15+")
    return {
        "rows": [{"key": k, "label": f"{k} days", "value": int(rows.get(k, 0))} for k in keys],
        "link": le_link(t, "/placements", status="pre_joining"),
    }


async def dropout_risk(t: TileContext) -> TileData:
    need(t, "placements")
    rows = (
        await t.db.execute(
            select(P.id, P.human_id, P.role_title, P.planned_start_date)
            .where(P.id.in_(scoped_placements(t, P.status.in_(PREJOIN), P.dropout_risk.is_(True))))
            .order_by(P.planned_start_date)
            .limit(20)
        )
    ).all()
    return {
        "value": len(rows),
        "rows": [
            {"placement": r.human_id, "role": r.role_title, "start": r.planned_start_date.isoformat()}
            for r in rows
        ],
        "row_links": [{"path": f"/placements/{r.id}", "query": {}} for r in rows],
        "link": le_link(t, "/placements", dropout_risk="true"),
    }


async def onboarding_cases_by_completion(t: TileContext) -> TileData:
    need(t, "onboarding_cases", "placements")
    oc = T.onboarding_cases.c
    q = case(
        (oc.progress_pct < 25, "0-24%"),
        (oc.progress_pct < 50, "25-49%"),
        (oc.progress_pct < 75, "50-74%"),
        else_="75-100%",
    )
    rows = dict(
        (
            await t.db.execute(
                select(q, func.count())
                .where(
                    oc.status.not_in(("completed", "cancelled")), oc.placement_id.in_(scoped_placements(t))
                )
                .group_by(q)
            )
        )
        .tuples()
        .all()
    )
    keys = ("0-24%", "25-49%", "50-74%", "75-100%")
    return {
        "rows": [{"key": k, "label": k, "value": int(rows.get(k, 0))} for k in keys],
        "link": le_link(t, "/onboarding"),
    }


async def start_gates_unmet_t3(t: TileContext) -> TileData:
    need(t, "placements", "start_gates")
    g = T.start_gates.c
    today = t.now.date()
    pending = (
        select(func.count())
        .where(g.placement_id == P.id, g.required.is_(True), g.status.in_(("pending", "failed")))
        .scalar_subquery()
    )
    rows = (
        await t.db.execute(
            select(P.id, P.human_id, P.planned_start_date, pending.label("pending"))
            .where(
                P.id.in_(
                    scoped_placements(
                        t, P.status.in_(PREJOIN), P.planned_start_date <= today + timedelta(days=3)
                    )
                ),
                pending > 0,
            )
            .order_by(P.planned_start_date)
            .limit(20)
        )
    ).all()
    return {
        "value": len(rows),
        "rows": [
            {
                "placement": r.human_id,
                "start": r.planned_start_date.isoformat(),
                "pending_gates": int(r.pending),
            }
            for r in rows
        ],
        "row_links": [{"path": f"/placements/{r.id}", "query": {}} for r in rows],
    }
