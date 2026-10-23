# ruff: file-ignore[line-too-long]
"""Internal talent matches (Epic 41 A5, FR-212).

* ``internal_candidate_ids`` -- the matching section ``internal`` (registered from ``hooks.py``): eligible internal
  Candidates (Workers with an active Placement + Users linked through ``mobility_employee_links``) for a Demand whose
  ``visibility`` is ``internal`` or ``both``; ``None`` (section hidden) for ``external``. P1 matching renders the
  section from its **stored** ``matches`` rows (AD-8: never recomputed here).
* ``fit_for_employee`` -- stored overall score as ``fit_pct`` + top 3 reasons (matched / missing skills from the
  evidence, which carries Epic 37's expanded skills). Compensation sub-scores and evidence are never shown to
  Employees. No stored match -> no fit ("Not scored yet"); the board never requests a recompute.
"""

import dataclasses
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.matching import Match

MAX_REASONS = 3


@dataclasses.dataclass(frozen=True, slots=True)
class Fit:
    fit_pct: int
    reasons: tuple[str, ...]


def fit_of(overall: Any, evidence: list[dict[str, Any]] | None, missing: list[str] | None) -> Fit:
    """Pure: the Employee-facing fit of one stored match (no compensation)."""
    items = [e for e in (evidence or []) if not e.get("comp") and e.get("kind") != "compensation" and e.get("text")]
    matched = [f"Matches {e['text']}" for e in items if e.get("kind") == "skill"]
    gaps = [f"Missing {m}" for m in (missing or [])]
    other = [str(e["text"]) for e in items if e.get("kind") not in ("skill", "similarity")]
    reasons = (matched[:2] + gaps[:1] + matched[2:] + gaps[1:] + other)[:MAX_REASONS]
    return Fit(round(float(overall)), tuple(reasons))


async def fit_for_employee(
    ctx: RequestContext | None,  # ruff: ignore[unused-function-argument] - contract signature
    db: AsyncSession,
    candidate_id: uuid.UUID | None,
    demand_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, Fit]:
    ids = list(set(demand_ids))
    if candidate_id is None or not ids:
        return {}
    rows = (
        await db.execute(
            select(Match.demand_id, Match.overall, Match.evidence, Match.missing_mandatory).where(
                Match.candidate_id == candidate_id, Match.demand_id.in_(ids)
            )
        )
    ).all()
    return {did: fit_of(overall, evidence, missing) for did, overall, evidence, missing in rows}


async def internal_candidate_ids(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> set[uuid.UUID] | None:
    """Eligible internal Candidates for the ``internal`` match section (None = no section for this Demand)."""
    from app.models.demands import Demand
    from app.models.placements import Placement
    from app.services.mobility import eligibility, employees
    from app.services.mobility.common import settings_row, today

    del ctx
    demand = await db.get(Demand, demand_id)
    if demand is None or demand.visibility not in ("internal", "both"):
        return None
    settings = await settings_row(db)
    rules = (await eligibility.load_rules(db, [demand_id])).get(demand_id)
    on = today()
    out: set[uuid.UUID] = set()
    # Only candidates with a stored match can be shown (AD-8): evaluate just those.
    scored = set((await db.execute(select(Match.candidate_id).where(Match.demand_id == demand_id))).scalars())
    if not scored:
        return out
    workers = set(
        (
            await db.execute(
                select(Placement.candidate_id).where(Placement.status == "active", Placement.candidate_id.in_(scored))
            )
        ).scalars()
    )
    for cid in sorted(workers, key=str):
        try:
            emp = await employees.worker_employee(db, cid)
        except Exception:
            continue
        if eligibility.evaluate(rules, settings.min_tenure_default_months, emp, on).eligible:
            out.add(cid)
    for cid, uid in (await employees.linked_user_candidates(db)).items():
        if cid in out or cid not in scored:
            continue
        try:
            emp = await employees.user_employee(db, uid)
        except Exception:
            continue
        if eligibility.evaluate(rules, settings.min_tenure_default_months, emp, on).eligible:
            out.add(cid)
    return out
