# ruff: file-ignore[line-too-long]
"""Skills supply and demand insight (Epic 37 A5, FR-191).

Per skill: ``open_demand`` (open / on-hold Demands the caller may read: ``demand:read`` scope OR assignee),
``supply_by_proficiency`` {1..5, unknown} and ``verified_supply`` (non-archived Candidates in ``candidate:read``
scope), ``bench`` (open bench entries of Workers with the skill; ``None`` without ``bench:read``) and ``trend_90d``
(13 weekly buckets of new demand-skill rows by ``demands.opened_at`` and new candidate-skill rows by
``created_at``, both scoped). Every count carries the filter params the UI uses to deep-link the lists.
"""

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.permissions import Principal, scope_condition
from app.models.candidates import Candidate, CandidateSkill
from app.models.demands import Demand, DemandSkill
from app.models.placements import BENCH_OPEN_STATUSES, BenchEntry
from app.schemas.skills import SkillInsightRow, SkillInsightsOut
from app.services.candidates import common as cand_common
from app.services.skills import graph as graph_mod

OPEN_LIKE = ("open", "on_hold")
WEEKS = 13
PROFICIENCY_BUCKETS = ("1", "2", "3", "4", "5", "unknown")


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _candidate_scope(principal: Principal) -> list[Any]:
    conds: list[Any] = [
        Candidate.archived_at.is_(None),
        Candidate.merged_into_id.is_(None),
        Candidate.erased_at.is_(None),
    ]
    cond = cand_common.read_condition(principal)
    if cond is not None:
        conds.append(cond)
    return conds


def _demand_scope(principal: Principal, business_unit_id: uuid.UUID | None) -> list[Any]:
    from app.services.demands.common import access_condition

    conds: list[Any] = [Demand.archived_at.is_(None)]
    cond = access_condition(principal, "demand:read")
    if cond is not None:
        conds.append(cond)
    if business_unit_id is not None:
        conds.append(Demand.business_unit_id == business_unit_id)
    return conds


def _cs_key(skill_id: uuid.UUID | None, tenant_skill_id: uuid.UUID | None) -> str:
    return graph_mod.ref_key(skill_id, tenant_skill_id)


def _ds_key(kind: str | None, skill_id: uuid.UUID) -> str:
    return graph_mod.tkey(skill_id) if kind == "tenant" else graph_mod.gkey(skill_id)


async def skill_insights(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    category: str | None = None,
    business_unit_id: uuid.UUID | None = None,
    skill_refs: list[str] | None = None,
    limit: int = 50,
) -> SkillInsightsOut:
    principal = cand_common.principal_of(ctx)
    wanted = {graph_mod.parse_key(k, "skill_refs").as_str() for k in skill_refs or []}
    can_demand = "demand:read" in principal.permissions
    can_candidate = "candidate:read" in principal.permissions
    now = datetime.now(UTC)
    first_week = _week_start(now.date()) - timedelta(weeks=WEEKS - 1)
    since = datetime(first_week.year, first_week.month, first_week.day, tzinfo=UTC)
    weeks = [first_week + timedelta(weeks=i) for i in range(WEEKS)]

    demand: dict[str, int] = defaultdict(int)
    demand_trend: dict[str, list[int]] = defaultdict(lambda: [0] * WEEKS)
    if can_demand:
        scope = _demand_scope(principal, business_unit_id)
        rows = await db.execute(
            select(DemandSkill.skill_ref_kind, DemandSkill.skill_id, func.count(func.distinct(Demand.id)))
            .join(Demand, Demand.id == DemandSkill.demand_id)
            .where(DemandSkill.skill_id.is_not(None), Demand.status.in_(OPEN_LIKE), *scope)
            .group_by(DemandSkill.skill_ref_kind, DemandSkill.skill_id)
        )
        for kind, sid, n in rows:
            demand[_ds_key(kind, sid)] += int(n)
        bucket = func.date_trunc("week", Demand.opened_at)
        rows = await db.execute(
            select(DemandSkill.skill_ref_kind, DemandSkill.skill_id, bucket, func.count())
            .join(Demand, Demand.id == DemandSkill.demand_id)
            .where(DemandSkill.skill_id.is_not(None), Demand.opened_at >= since, *scope)
            .group_by(DemandSkill.skill_ref_kind, DemandSkill.skill_id, bucket)
        )
        for kind, sid, wk, n in rows:
            idx = (wk.date() - first_week).days // 7
            if 0 <= idx < WEEKS:
                demand_trend[_ds_key(kind, sid)][idx] += int(n)

    supply: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(PROFICIENCY_BUCKETS, 0))
    verified: dict[str, int] = defaultdict(int)
    supply_trend: dict[str, list[int]] = defaultdict(lambda: [0] * WEEKS)
    bench: dict[str, int] | None = None
    if can_candidate:
        cscope = _candidate_scope(principal)
        rows = await db.execute(
            select(
                CandidateSkill.skill_id,
                CandidateSkill.tenant_skill_id,
                CandidateSkill.proficiency,
                CandidateSkill.verification,
                func.count(),
            )
            .join(Candidate, Candidate.id == CandidateSkill.candidate_id)
            .where(*cscope)
            .group_by(
                CandidateSkill.skill_id,
                CandidateSkill.tenant_skill_id,
                CandidateSkill.proficiency,
                CandidateSkill.verification,
            )
        )
        for gid, tid, prof, ver, n in rows:
            key = _cs_key(gid, tid)
            supply[key][str(prof) if prof else "unknown"] += int(n)
            if ver == "verified":
                verified[key] += int(n)
        bucket = func.date_trunc("week", CandidateSkill.created_at)
        rows = await db.execute(
            select(CandidateSkill.skill_id, CandidateSkill.tenant_skill_id, bucket, func.count())
            .join(Candidate, Candidate.id == CandidateSkill.candidate_id)
            .where(CandidateSkill.created_at >= since, *cscope)
            .group_by(CandidateSkill.skill_id, CandidateSkill.tenant_skill_id, bucket)
        )
        for gid, tid, wk, n in rows:
            idx = (wk.date() - first_week).days // 7
            if 0 <= idx < WEEKS:
                supply_trend[_cs_key(gid, tid)][idx] += int(n)
        if "bench:read" in principal.permissions:
            bench = defaultdict(int)
            bscope = scope_condition(principal, "bench:read", Candidate.owner_id)
            stmt = (
                select(
                    CandidateSkill.skill_id,
                    CandidateSkill.tenant_skill_id,
                    func.count(func.distinct(BenchEntry.candidate_id)),
                )
                .join(BenchEntry, and_(BenchEntry.candidate_id == CandidateSkill.candidate_id))
                .join(Candidate, Candidate.id == CandidateSkill.candidate_id)
                .where(BenchEntry.status.in_(BENCH_OPEN_STATUSES))
                .group_by(CandidateSkill.skill_id, CandidateSkill.tenant_skill_id)
            )
            if bscope is not None:
                stmt = stmt.where(bscope)
            for gid, tid, n in await db.execute(stmt):
                bench[_cs_key(gid, tid)] += int(n)

    keys = set(wanted) if wanted else set(demand) | set(supply)
    names = await graph_mod.labels(db, keys) if keys else {}
    if category:
        keys = {k for k in keys if (names[k].category or "").lower() == category.lower()}

    def total(k: str) -> int:
        return sum(supply[k].values()) if k in supply else 0

    ranked = sorted(keys, key=lambda k: (-demand.get(k, 0), -total(k), names[k].name.lower(), k))[
        : max(1, min(limit, 200))
    ]
    items: list[SkillInsightRow] = []
    for k in ranked:
        skill_id = k.split(":", 1)[1]
        items.append(
            SkillInsightRow(
                skill_key=k,
                name=names[k].name,
                category=names[k].category,
                open_demand=demand.get(k, 0),
                supply_by_proficiency=dict(supply[k])
                if k in supply
                else dict.fromkeys(PROFICIENCY_BUCKETS, 0),
                supply_total=total(k),
                verified_supply=verified.get(k, 0),
                bench=None if bench is None else bench.get(k, 0),
                trend_90d={
                    "demand": list(demand_trend.get(k, [0] * WEEKS)),
                    "supply": list(supply_trend.get(k, [0] * WEEKS)),
                },
                links={
                    "demands": {"status": ["open", "on_hold"], "skill_id": skill_id},
                    "candidates": {"skills": [skill_id], "skills_mode": "all"},
                    "verified": {"skills": [skill_id], "skills_mode": "all", "verification": "verified"},
                    "bench": {"skill_id": skill_id},
                },
            )
        )
    return SkillInsightsOut(items=items, generated_at=now, week_starts=weeks)
