# ruff: file-ignore[line-too-long]
"""Immigration dashboard tiles (Epic 52 A4; Epic 46 data). Every number comes from the owner's data-scoped
dashboard read (``immigration.dashboard.summary`` / ``at_risk``: ``immigration:read`` on owner / BU), so tiles equal
the owner's ``/immigration`` page and its list filters."""

from sqlalchemy import func, select

from app.models.immigration import ImmigrationCase, Permit
from app.services.reporting.metrics import TileContext, TileData, link
from app.services.reporting.metrics.p3common import need

CATEGORIES = ("preparing", "filed", "pending_decision")


async def cases_by_stage_category(t: TileContext) -> TileData:
    need(t, "immigration_cases")
    from app.services.immigration import dashboard

    out = await dashboard.summary(t.ctx, t.db)
    counts: dict[str, int] = {}
    for s in out.by_stage:
        counts[s.category] = counts.get(s.category, 0) + s.count
    keys = ("open", *CATEGORIES)
    rows = [
        {
            "key": k,
            "label": "Not started" if k == "open" else k.replace("_", " ").capitalize(),
            "value": counts.get(k, 0),
        }
        for k in keys
    ]
    return {
        "value": sum(r["value"] for r in rows),
        "rows": rows,
        "link": link("/immigration/cases", {"status": "open_any"}),
    }


async def permits_expiring(t: TileContext) -> TileData:
    """Non-superseded Permits expiring within 30 / 60 / 90 days (the P2 credential expiry alerts fire from the
    Credentials the owner records for each Permit)."""
    need(t, "permits", "immigration_cases")
    from app.services.immigration.common import scoped

    on = t.now.date()
    from datetime import timedelta

    in_scope = scoped(t.ctx, select(ImmigrationCase.id))
    p = Permit
    n30, n60, n90 = (
        await t.db.execute(
            select(
                func.count().filter(p.valid_to <= on + timedelta(days=30)),
                func.count().filter(p.valid_to <= on + timedelta(days=60)),
                func.count(),
            ).where(
                p.superseded_by_id.is_(None),
                p.valid_to.is_not(None),
                p.valid_to >= on,
                p.valid_to <= on + timedelta(days=90),
                p.case_id.in_(in_scope),
            )
        )
    ).one()
    days = ("30", "60", "90")
    return {
        "rows": [
            {"key": d, "label": f"{d} days", "value": int(v)}
            for d, v in zip(days, (n30, n60, n90), strict=True)
        ],
        "row_links": [link("/immigration/cases", {"permits_expiring_days": d}) for d in days],
    }


async def worker_days_without_permit(t: TileContext) -> TileData:
    """SM-28 (target 0): Workers abroad today without a valid Permit (owner rule ``dashboard.at_risk``)."""
    need(t, "immigration_cases", "permits")
    from app.services.immigration import dashboard

    risk = await dashboard.at_risk(t.ctx, t.db)
    return {
        "value": len(risk),
        "unit": "workers",
        "rows": [
            {"placement": r.placement_human_id, "work_country": r.work_country, "case": r.case_human_id}
            for r in risk[:20]
        ],
        "link": link("/immigration/cases", {"at_risk": "true"}),
    }
