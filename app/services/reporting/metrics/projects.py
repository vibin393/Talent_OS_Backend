# ruff: noqa: E501
"""Projects dashboard tiles (Epic 52 A4; Epic 45 data). Projects are scoped like ``GET /projects``
(``projects.common.scoped``: owner / BU). Burn is the owner's ``project_burn`` projection, computed from posted facts
only (AD-33); money is shown only with ``compensation:view`` (counts stay)."""

from typing import Any

from sqlalchemy import func, select

from app.models.projects import Deliverable, Project, ProjectBudget, ProjectBurn
from app.services.reporting.metrics import TileContext, TileData, link
from app.services.reporting.metrics.p3common import comp, money, need

THRESHOLD_PCT = 80


def scoped_ids(t: TileContext, *conds: Any) -> Any:
    from app.services.projects.common import scoped

    return scoped(t.ctx, select(Project.id).where(Project.archived_at.is_(None), *conds), "project:read")


def _latest_burn() -> Any:
    ranked = (
        select(
            ProjectBurn.project_id, ProjectBurn.actual, ProjectBurn.budget, ProjectBurn.pct, ProjectBurn.currency,
            func.row_number().over(partition_by=ProjectBurn.project_id, order_by=ProjectBurn.as_of.desc()).label("rn"),
        )
        .where(ProjectBurn.dimension == "cost")
        .subquery()
    )  # fmt: skip
    return select(ranked).where(ranked.c.rn == 1).subquery()


async def _burn_rows(t: TileContext, *conds: Any, limit: int = 20) -> list[Any]:
    b = _latest_burn()
    budget = (
        select(ProjectBudget.project_id, ProjectBudget.amount)
        .where(ProjectBudget.dimension == "cost")
        .subquery()
    )
    stmt = (
        select(
            Project.id, Project.human_id, Project.name, Project.currency, budget.c.amount, b.c.actual, b.c.pct
        )
        .outerjoin(budget, budget.c.project_id == Project.id)
        .outerjoin(b, b.c.project_id == Project.id)
        .where(Project.id.in_(scoped_ids(t, Project.status.in_(("active", "on_hold")))), *conds)
        .order_by(b.c.pct.desc().nulls_last(), Project.human_id)
        .limit(limit)
    )
    return list((await t.db.execute(stmt)).all())


def _row(t: TileContext, r: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "project": f"{r.human_id} {r.name}",
        "pct_used": None if r.pct is None else float(r.pct),
    }
    if comp(t):
        out["budget"] = money(r.amount, r.currency)
        out["burn"] = money(r.actual, r.currency)
    return out


async def project_burn(t: TileContext) -> TileData:
    need(t, "projects", "project_burn", "project_budgets")
    rows = await _burn_rows(t)
    return {
        "value": len(rows),
        "rows": [_row(t, r) for r in rows],
        "row_links": [link(f"/projects/{r.id}") for r in rows],
        "masked": not comp(t),
        "link": link("/projects", {"status": "active"}),
    }


async def deliverables_awaiting_acceptance(t: TileContext) -> TileData:
    need(t, "deliverables", "projects")
    n = int(
        (
            await t.db.execute(
                select(func.count()).where(
                    Deliverable.status == "submitted", Deliverable.project_id.in_(scoped_ids(t))
                )
            )
        ).scalar_one()
    )
    return {"value": n, "unit": "deliverables", "link": link("/projects", {"deliverables": "submitted"})}


async def budget_threshold_breaches(t: TileContext) -> TileData:
    need(t, "projects", "project_burn", "project_budgets")
    b = _latest_burn()
    rows = await _burn_rows(t, Project.id.in_(select(b.c.project_id).where(b.c.pct >= THRESHOLD_PCT)))
    return {
        "value": len(rows),
        "rows": [{**_row(t, r), "threshold": "100%" if (r.pct or 0) >= 100 else "80%"} for r in rows],
        "row_links": [link(f"/projects/{r.id}") for r in rows],
        "masked": not comp(t),
    }
