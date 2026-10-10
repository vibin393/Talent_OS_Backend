"""Account manager dashboard tiles (clients the caller owns, or the BU scope)."""

from typing import Any

from sqlalchemy import func, select

from app.models.clients import Client
from app.models.demands import Demand
from app.models.pipeline import Application, Submission
from app.services.reporting.common import has_interviews, interviews
from app.services.reporting.lists import INTERVIEW_ACTIVE_STATUSES, stage_ids
from app.services.reporting.metrics import TileContext, TileData, count_link, ids_of, link, list_query, pct
from app.services.reporting.metrics.manager import filled_join, hire_ranks

FEEDBACK_BUSINESS_DAYS = 3


async def demands_by_client(t: TileContext) -> TileData:
    f = t.filters
    lq = await list_query(t, "demands", dict(t.scope_query()))
    ranked = hire_ranks(ids_of(lq), f.end)
    hires = (
        select(func.count())
        .select_from(Application)
        .where(
            Application.demand_id == Demand.id,
            Application.archived_at.is_(None),
            Application.stage_id.in_(stage_ids("hired")),
        )
        .correlate(Demand)
        .scalar_subquery()
    )
    submissions = (
        select(func.count())
        .select_from(Submission)
        .where(Submission.demand_id == Demand.id, Submission.sent_at >= f.start, Submission.sent_at < f.end)
        .correlate(Demand)
        .scalar_subquery()
    )
    per_demand: list[Any] = [
        Demand.client_id,
        Demand.status,
        Demand.positions,
        ranked.c.hired_at.label("filled_at"),
        hires.label("hires"),
        submissions.label("submissions"),
    ]
    if has_interviews(t.avail):
        per_demand.append(
            select(func.count())
            .select_from(interviews)
            .where(
                interviews.c.demand_id == Demand.id,
                interviews.c.status.in_(INTERVIEW_ACTIVE_STATUSES),
                interviews.c.starts_at >= f.start,
                interviews.c.starts_at < f.end,
            )
            .correlate(Demand)
            .scalar_subquery()
            .label("interviews")
        )
    d = (
        select(*per_demand)
        .select_from(Demand)
        .outerjoin(ranked, filled_join(ranked))
        .where(Demand.id.in_(ids_of(lq)))
        .subquery()
    )
    active = d.c.status.in_(("open", "filled"))
    cols: list[Any] = [
        Client.id,
        Client.name,
        func.count().filter(d.c.status == "open").label("open"),
        func.count().filter(d.c.status == "on_hold").label("on_hold"),
        func.count()
        .filter(d.c.status == "filled", d.c.filled_at >= f.start, d.c.filled_at < f.end)
        .label("filled"),
        func.coalesce(func.sum(d.c.positions).filter(active), 0).label("positions"),
        func.coalesce(func.sum(d.c.hires).filter(active), 0).label("hires"),
        func.coalesce(func.sum(d.c.submissions), 0).label("submissions"),
    ]
    if has_interviews(t.avail):
        cols.append(func.coalesce(func.sum(d.c.interviews), 0).label("interviews"))
    result = await t.db.execute(
        select(*cols)
        .select_from(d)
        .join(Client, Client.id == d.c.client_id)
        .group_by(Client.id, Client.name)
        .order_by(func.count().filter(d.c.status == "open").desc(), Client.name, Client.id)
        .limit(50)
    )
    rows: list[dict[str, Any]] = []
    for r in result.mappings():
        positions, hired = int(r["positions"]), int(r["hires"])
        rows.append(
            {
                "client_id": str(r["id"]),
                "client": r["name"],
                "open": int(r["open"]),
                "on_hold": int(r["on_hold"]),
                "filled": int(r["filled"]),
                "positions": positions,
                "hires": hired,
                "fill_progress": pct(min(hired, positions) if positions else hired, positions),
                "submissions": int(r["submissions"]),
                "interviews": int(r["interviews"]) if "interviews" in r else None,
            }
        )
    return {
        "value": len(rows),
        "rows": rows,
        "link": link("/demands", dict(t.scope_query())),
        "row_links": [link("/demands", {"client_id": r["client_id"]}) for r in rows],
    }


async def awaiting_feedback(t: TileContext) -> TileData:
    value, lk = await count_link(
        t, "submissions", {"feedback": "none", "older_than": FEEDBACK_BUSINESS_DAYS, **t.scope_query()}
    )
    return {"value": value, "link": lk}


async def offers_pending(t: TileContext) -> TileData:
    value, lk = await count_link(t, "offers", {"status": "released,viewed", **t.scope_query()})
    return {"value": value, "link": lk}


__all__ = ["awaiting_feedback", "demands_by_client", "offers_pending"]
