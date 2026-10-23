# ruff: file-ignore[line-too-long]
"""Time-to-fill extractor (Epic 48 A3, FR-241).

* Subjects scored: Demands ``open``.
* Label: days from ``opened_at`` to the first Placement created for the Demand; the model target is
  ``log(days + 1)``. Demands closed / cancelled without a Placement are excluded [ASSUMPTION: no censoring model
  in Phase 3].
* Training ``as_of`` = ``opened_at``.

``extract`` is pure: it reads only facts timestamped <= ``as_of`` (client feedback delays, prior fills, recruiter
assignments). Demand attributes (skills, openings, priority, rate) are taken as recorded.
"""

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.analytics.features import common
from app.services.analytics.features.common import DemandFact, FeatureRow, ScoringRow, TrainingRow, median

PRIORITY_RANK = {"low": 0.0, "medium": 1.0, "high": 2.0, "critical": 3.0}


@dataclasses.dataclass(frozen=True, slots=True)
class Context:
    """Tenant-level facts shared by every Demand (loaded once per run)."""

    candidate_total: int
    skill_holders: Mapping[uuid.UUID, int]
    feedback_delays: Mapping[uuid.UUID, Sequence[tuple[datetime, float]]]
    client_demands: Mapping[uuid.UUID, Sequence[DemandFact]]
    rate_medians: Mapping[tuple[str | None, str | None], float]
    assignments: Mapping[uuid.UUID, Sequence[tuple[uuid.UUID, datetime, datetime | None]]]
    demand_open: Mapping[uuid.UUID, tuple[datetime | None, datetime | None]]


def skill_rarity(
    skill_ids: Sequence[uuid.UUID], holders: Mapping[uuid.UUID, int], total: int
) -> float | None:
    if not skill_ids or total <= 0:
        return None
    inv = [total / max(holders.get(s, 0), 1) for s in skill_ids]
    return sum(inv) / len(inv)


def extract(d: DemandFact, as_of: datetime, ctx: Context) -> FeatureRow:
    window = as_of - timedelta(days=365)
    delays = [v for at, v in ctx.feedback_delays.get(d.client_id, ()) if window < at <= as_of]
    prior = [
        p
        for p in ctx.client_demands.get(d.client_id, ())
        if p.id != d.id and p.opened_at is not None and p.opened_at <= as_of
    ]
    resolved = [
        p
        for p in prior
        if (p.filled_at is not None and p.filled_at <= as_of)
        or (p.closed_at is not None and p.closed_at <= as_of)
    ]
    filled = [p for p in resolved if p.filled_at is not None and p.filled_at <= as_of]
    load = None
    if d.primary_user_id is not None:
        load = 0.0
        for demand_id, assigned_at, removed_at in ctx.assignments.get(d.primary_user_id, ()):
            if demand_id == d.id or assigned_at > as_of or (removed_at is not None and removed_at <= as_of):
                continue
            opened, closed = ctx.demand_open.get(demand_id, (None, None))
            if opened is not None and opened <= as_of and (closed is None or closed > as_of):
                load += 1.0
    med = ctx.rate_medians.get(d.rate_key)
    return {
        "required_skills": float(d.required_skills),
        "skill_rarity": skill_rarity(d.skill_ids, ctx.skill_holders, ctx.candidate_total),
        "min_experience": d.min_experience,
        "openings": float(d.positions),
        "priority": PRIORITY_RANK.get(d.priority, 1.0),
        "employment_type": d.employment_type,
        "remote": 1.0 if d.work_mode == "remote" else 0.0,
        "city": d.city,
        "client_feedback_delay": median(delays),
        "client_fill_rate": (len(filled) / len(resolved)) if resolved else None,
        "rate_vs_median": (d.bill_rate / med) if d.bill_rate is not None and med else None,
        "recruiter_load": load,
    }


def label(d: DemandFact) -> tuple[float, datetime] | None:
    """(days to fill, outcome_at) or None while unknown / never filled."""
    if d.opened_at is None or d.filled_at is None or d.filled_at < d.opened_at:
        return None
    return (d.filled_at - d.opened_at).total_seconds() / 86400.0, d.filled_at


async def _context(db: AsyncSession, demands: Sequence[DemandFact]) -> Context:
    everything = await common.load_demands(db)
    by_client: dict[uuid.UUID, list[DemandFact]] = {}
    for d in everything:
        by_client.setdefault(d.client_id, []).append(d)
    skill_ids = {s for d in demands for s in d.skill_ids}
    return Context(
        candidate_total=await common.tenant_candidate_count(db),
        skill_holders=await common.skill_holder_counts(db, skill_ids),
        feedback_delays=await common.client_feedback_delays(db),
        client_demands=by_client,
        rate_medians=await common.demand_rate_medians(db),
        assignments=await common.assignment_intervals(db),
        demand_open={d.id: (d.opened_at, d.filled_at or d.closed_at) for d in everything},
    )


async def training_rows(db: AsyncSession, now: datetime, history_months: int) -> list[TrainingRow]:
    since = now - timedelta(days=30 * history_months)
    demands = [d for d in await common.load_demands(db) if d.opened_at is not None and d.opened_at >= since]
    ctx = await _context(db, demands)
    rows: list[TrainingRow] = []
    for d in demands:
        lab = label(d)
        if lab is None or d.opened_at is None:
            continue
        days, outcome_at = lab
        rows.append(TrainingRow(d.id, None, d.opened_at, outcome_at, extract(d, d.opened_at, ctx), days))
    return rows


async def scoring_rows(
    db: AsyncSession, now: datetime, subject_ids: Sequence[uuid.UUID] | None
) -> list[ScoringRow]:
    demands = [d for d in await common.load_demands(db, subject_ids) if d.status == "open"]
    if not demands:
        return []
    ctx = await _context(db, demands)
    return [ScoringRow("demand", d.id, None, extract(d, now, ctx)) for d in demands]


def live_subject(status: str) -> bool:
    return status == "open"


def describe() -> dict[str, Any]:
    return {"subjects": "open demands", "label": "days from opening to the first placement"}
