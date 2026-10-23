# ruff: file-ignore[line-too-long]
"""Skills dashboard tiles (Epic 52 A4; Epic 37 data, ``skills_insight:read``). Supply / demand comes from the owner's
``skills_service.skill_insights`` (graph-expanded through ``skills_service.expand``, scoped by the caller's demand /
candidate read permissions)."""

from sqlalchemy import func, select

from app.core.permissions import scope_condition
from app.models.candidates import Candidate, CandidateSkill
from app.services.reporting.metrics import TileContext, TileData, pct
from app.services.reporting.metrics.p3common import need


async def skills_supply_demand(t: TileContext) -> TileData:
    from app.services import skills_service

    out = await skills_service.skill_insights(t.ctx, t.db, limit=20)
    rows = [
        {
            "skill": r.name,
            "category": r.category,
            "open_demand": r.open_demand,
            "supply": r.supply_total,
            "verified_supply": r.verified_supply,
        }
        for r in out.items[:20]
    ]
    return {"value": len(rows), "rows": rows, "link": {"path": "/reports/skills", "query": {}}}


async def verified_skill_share(t: TileContext) -> TileData:
    need(t, "candidate_skills")
    stmt = select(func.count(), func.count().filter(CandidateSkill.verification == "verified")).select_from(
        CandidateSkill
    )
    cond = scope_condition(t.principal, "candidate:read", Candidate.owner_id)
    if cond is not None:
        stmt = stmt.where(CandidateSkill.candidate_id.in_(select(Candidate.id).where(cond)))
    total, verified = (await t.db.execute(stmt)).one()
    return {"value": pct(int(verified), int(total)), "unit": "%", "n": int(total)}
