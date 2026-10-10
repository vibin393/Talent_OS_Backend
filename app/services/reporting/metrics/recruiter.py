"""Recruiter dashboard tiles ("my" = demands the user is an active assignee of, applications the user owns
or that sit on those demands). The BU filter does not apply to these tiles."""

from datetime import timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select

from app.models.clients import Client
from app.models.demands import Demand, DemandSlaClock
from app.models.pipeline import Application, PipelineStage
from app.services.reporting.common import has_interviews, has_offers, iso_week, my_demand_ids
from app.services.reporting.lists import TERMINAL_STAGE_KEYS, demand_condition, stage_ids
from app.services.reporting.metrics import TileContext, TileData, count_link, link, list_query

SLA_SEVERITY = {"breached": 3, "at_risk": 2, "healthy": 1}
SLA_BY_RANK = {v: k for k, v in SLA_SEVERITY.items()}


async def my_open_demands(t: TileContext) -> TileData:
    worst = (
        select(func.max(case(SLA_SEVERITY, value=DemandSlaClock.status, else_=0)))
        .where(DemandSlaClock.demand_id == Demand.id, DemandSlaClock.completed_at.is_(None))
        .scalar_subquery()
    )
    pipeline = (
        select(func.count())
        .select_from(Application)
        .where(
            Application.demand_id == Demand.id,
            Application.archived_at.is_(None),
            Application.stage_id.not_in(stage_ids(*TERMINAL_STAGE_KEYS)),
        )
        .scalar_subquery()
    )
    client = select(Client.name).where(Client.id == Demand.client_id).scalar_subquery()
    # Same rows as ``GET /demands?status=open&assignee=me`` (scope + assignment + not archived).
    lq = await list_query(t, "demands", {"status": ["open"], "assignee": "me"})
    total = int((await t.db.execute(lq.count())).scalar_one())
    severity = func.coalesce(worst, 0)
    rows = (
        await t.db.execute(
            select(
                Demand.id,
                Demand.req_id,
                Demand.title,
                client.label("client"),
                severity.label("severity"),
                Demand.opened_at,
                pipeline.label("pipeline_count"),
            )
            .where(Demand.id.in_(lq.base.order_by(None)))
            .order_by(severity.desc(), Demand.opened_at.asc().nulls_last(), Demand.req_id)
            .limit(20)
        )
    ).all()
    out = [
        {
            "id": str(r.id),
            "req_id": r.req_id,
            "title": r.title,
            "client": r.client,
            "sla_status": SLA_BY_RANK.get(int(r.severity)),
            "days_open": (t.now - r.opened_at).days if r.opened_at else None,
            "pipeline_count": int(r.pipeline_count),
        }
        for r in rows
    ]
    return {
        "value": total,
        "rows": out,
        "link": link("/demands", {"status": "open", "assignee": "me"}),
        "row_links": [link(f"/demands/{r['id']}") for r in out],
    }


async def my_pipeline_by_stage(t: TileContext) -> TileData:
    lq = await list_query(t, "applications", {"scope": "mine", "active": True})
    counts = (
        select(Application.stage_id, func.count().label("n"))
        .where(Application.id.in_(lq.base.order_by(None)))
        .group_by(Application.stage_id)
        .subquery()
    )
    rows = (
        await t.db.execute(
            select(PipelineStage.key, PipelineStage.label, func.coalesce(counts.c.n, 0).label("n"))
            .select_from(PipelineStage)
            .outerjoin(counts, counts.c.stage_id == PipelineStage.id)
            .where(
                PipelineStage.key.not_in(TERMINAL_STAGE_KEYS),
                or_(PipelineStage.archived_at.is_(None), counts.c.n > 0),
            )
            .order_by(PipelineStage.position, PipelineStage.key)
        )
    ).all()
    out = [{"key": r.key, "label": r.label, "value": int(r.n)} for r in rows]
    base = {"scope": "mine", "active": True}
    return {
        "value": sum(r["value"] for r in out),
        "rows": out,
        "link": link("/reports/applications", base),
        "row_links": [link("/reports/applications", {**base, "stage": r["key"]}) for r in out],
    }


async def tasks_due(t: TileContext) -> TileData:
    from app.services import tasks_service

    summary = await tasks_service.summary(t.ctx, t.db)
    links = [
        link("/tasks", {"view": "today", "owner": "me"}),
        link("/tasks", {"view": "overdue", "owner": "me"}),
    ]
    return {
        "value": summary.due_today,
        "rows": [
            {"key": "due_today", "label": "Due today", "value": summary.due_today},
            {"key": "overdue", "label": "Overdue", "value": summary.overdue},
        ],
        "link": links[0],
        "row_links": links,
    }


async def interviews_today(t: TileContext) -> TileData:
    lq = await list_query(t, "interviews", {"mine": True, "date": "today", "sort": "starts_at"})
    total = int((await t.db.execute(lq.count())).scalar_one())
    result = await t.db.execute(lq.rows.limit(10))
    rows: list[dict[str, Any]] = []
    for r in result.mappings():
        rows.append(
            {
                "id": str(r["id"]),
                "starts_at": r["starts_at"].isoformat() if r["starts_at"] else None,
                "candidate": r["candidate"],
                "demand_req_id": r["demand_req_id"],
                "demand_title": r["demand_title"],
                "stage_name": r["stage_name"],
                "mode": r["mode"],
                "status": r["status"],
                "application_id": str(r["application_id"]),
            }
        )
    return {
        "value": total,
        "rows": rows,
        "link": link("/reports/interviews", {"mine": True, "date": "today"}),
        "row_links": [link(f"/applications/{r['application_id']}") for r in rows],
    }


async def my_week(t: TileContext) -> TileData:
    monday, sunday = iso_week(t.now, t.filters.tz)
    week = {"from": monday.isoformat(), "to": sunday.isoformat()}
    rows: list[dict[str, Any]] = []
    links: list[dict[str, Any] | None] = []
    value, lk = await count_link(t, "submissions", {"sent_by": "me", **week})
    rows.append({"key": "submissions", "label": "Submissions sent", "value": value})
    links.append(lk)
    if has_interviews(t.avail):
        value, lk = await count_link(t, "interviews", {"mine": True, **week})
        rows.append({"key": "interviews", "label": "Interviews scheduled", "value": value})
        links.append(lk)
    if has_offers(t.avail):
        value, lk = await count_link(t, "offers", {"mine": True, **week})
        rows.append({"key": "offers", "label": "Offers released", "value": value})
        links.append(lk)
    return {
        "value": rows[0]["value"],
        "rows": rows,
        "link": links[0],
        "row_links": links,
        "date_from": monday,
        "date_to": sunday,
    }


__all__ = [
    "and_",
    "demand_condition",
    "interviews_today",
    "my_demand_ids",
    "my_open_demands",
    "my_pipeline_by_stage",
    "my_week",
    "tasks_due",
    "timedelta",
]
