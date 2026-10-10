# ruff: noqa: E501
"""Selection points (Epic 49 A5, FR-247): public read queries of pipeline, matching, screenings and the Epic 40
assessment hub. Each returns, for the date range and filters, one ``Point(candidate_id, selected, score)`` per
(candidate, demand) -- ids stay in the task's memory only and never reach the snapshot.

* ``stage_transition {from_stage_key?, to_stage_key}`` -- applicants: applications that entered ``from`` (or were
  created, without ``from``) in range; selected: reached ``to_stage_key`` (``application_stage_history``).
* ``ai_match_threshold {min_score}`` -- stored ``matches`` computed in range; selected: ``overall >= min_score``;
  ``score`` = overall (scoring-rate method).
* ``ai_screening_pass`` -- AI screenings completed in range; selected: ``outcome = 'proceed'``.
* ``assessment_pass {assessment_id?}`` -- Epic 40 invitations decided (``passed`` / ``failed``) in range; selected:
  ``result = 'pass'``; ``score`` 0-100.
"""

import dataclasses
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessments import AssessmentInvitation
from app.models.demands import Demand
from app.models.matching import Match
from app.models.pipeline import Application, ApplicationStageHistory, PipelineStage, Screening


@dataclasses.dataclass(frozen=True, slots=True)
class Point:
    candidate_id: uuid.UUID
    demand_id: uuid.UUID | None
    selected: bool
    score: Decimal | None = None


SCORE_BASED = frozenset({"ai_match_threshold", "assessment_pass"})
DEFINITIONS = {
    "stage_transition": "Applicants are applications that entered the 'from' stage (or were created) in the date range; selected applicants reached the 'to' stage.",
    "ai_match_threshold": "Applicants are AI Match scores computed in the date range; selected applicants scored at or above the threshold. Scoring rate: share scoring above the sample median.",
    "ai_screening_pass": "Applicants are AI screenings completed in the date range; selected applicants have the outcome 'proceed'.",
    "assessment_pass": "Applicants are assessment invitations decided (passed or failed) in the date range; selected applicants passed. Scoring rate: share scoring above the sample median.",
}


def _bounds(date_from: date, date_to: date) -> tuple[datetime, datetime]:
    return datetime.combine(date_from, time.min, UTC), datetime.combine(
        date_to + timedelta(days=1), time.min, UTC
    )


def _demand_filter(filters: dict[str, Any], demand_col: Any) -> list[Any]:
    conds: list[Any] = []
    ids = [uuid.UUID(str(x)) for x in filters.get("demand_ids") or []]
    if ids:
        conds.append(demand_col.in_(ids))
    sub_conds: list[Any] = []
    if filters.get("client_id"):
        sub_conds.append(Demand.client_id == uuid.UUID(str(filters["client_id"])))
    if filters.get("job_category"):
        sub_conds.append(func.lower(Demand.department) == str(filters["job_category"]).strip().lower())
    if sub_conds:
        conds.append(demand_col.in_(select(Demand.id).where(and_(*sub_conds))))
    return conds


async def _stage_id(db: AsyncSession, key: str | None) -> uuid.UUID | None:
    if not key:
        return None
    sid = (await db.execute(select(PipelineStage.id).where(PipelineStage.key == key))).scalar_one_or_none()
    if sid is None:
        from app.services.fairness.common import invalid

        raise invalid("fairness_unknown_stage", f"Unknown pipeline stage '{key}'", "params")
    return sid


async def stage_transition(
    db: AsyncSession, params: dict[str, Any], lo: datetime, hi: datetime, filters: dict[str, Any]
) -> list[Point]:
    from_id = await _stage_id(db, params.get("from_stage_key"))
    to_id = await _stage_id(db, params.get("to_stage_key"))
    if from_id is not None:
        entered = (
            select(ApplicationStageHistory.application_id)
            .where(
                ApplicationStageHistory.to_stage_id == from_id,
                ApplicationStageHistory.at >= lo,
                ApplicationStageHistory.at < hi,
            )
            .distinct()
        )
        base = select(Application.id, Application.candidate_id, Application.demand_id).where(
            Application.id.in_(entered)
        )
    else:
        base = select(Application.id, Application.candidate_id, Application.demand_id).where(
            Application.created_at >= lo, Application.created_at < hi
        )
    for cond in _demand_filter(filters, Application.demand_id):
        base = base.where(cond)
    rows = (await db.execute(base)).tuples().all()
    if not rows:
        return []
    reached = (
        select(ApplicationStageHistory.application_id)
        .where(ApplicationStageHistory.to_stage_id == to_id)
        .distinct()
    )
    current = select(Application.id).where(Application.stage_id == to_id)
    hit = set((await db.execute(reached)).scalars()) | set((await db.execute(current)).scalars())
    return [Point(cid, did, aid in hit) for aid, cid, did in rows]


async def ai_match_threshold(
    db: AsyncSession, params: dict[str, Any], lo: datetime, hi: datetime, filters: dict[str, Any]
) -> list[Point]:
    threshold = Decimal(str(params.get("min_score") or 0))
    stmt = select(Match.candidate_id, Match.demand_id, Match.overall).where(
        Match.computed_at >= lo, Match.computed_at < hi
    )
    for cond in _demand_filter(filters, Match.demand_id):
        stmt = stmt.where(cond)
    return [
        Point(cid, did, Decimal(o) >= threshold, Decimal(o))
        for cid, did, o in (await db.execute(stmt)).tuples()
    ]


async def ai_screening_pass(
    db: AsyncSession,
    params: dict[str, Any],  # noqa: ARG001
    lo: datetime,
    hi: datetime,
    filters: dict[str, Any],
) -> list[Point]:
    stmt = (
        select(Screening.candidate_id, Application.demand_id, Screening.outcome)
        .join(Application, Application.id == Screening.application_id)
        .where(
            Screening.kind == "ai",
            Screening.status == "completed",
            Screening.completed_at >= lo,
            Screening.completed_at < hi,
        )
        .order_by(Screening.completed_at)
    )
    for cond in _demand_filter(filters, Application.demand_id):
        stmt = stmt.where(cond)
    seen: dict[tuple[uuid.UUID, uuid.UUID], Point] = {}
    for cid, did, outcome in (await db.execute(stmt)).tuples():
        seen.setdefault((cid, did), Point(cid, did, outcome == "proceed"))  # first qualifying screening
    return list(seen.values())


async def assessment_pass(
    db: AsyncSession, params: dict[str, Any], lo: datetime, hi: datetime, filters: dict[str, Any]
) -> list[Point]:
    when = func.coalesce(AssessmentInvitation.decided_at, AssessmentInvitation.scored_at)
    stmt = (
        select(
            AssessmentInvitation.candidate_id,
            Application.demand_id,
            AssessmentInvitation.result,
            AssessmentInvitation.score,
        )
        .outerjoin(Application, Application.id == AssessmentInvitation.application_id)
        .where(
            AssessmentInvitation.candidate_id.is_not(None),
            AssessmentInvitation.status.in_(("passed", "failed")),
            when >= lo,
            when < hi,
        )
        .order_by(when)
    )
    if params.get("assessment_id"):
        stmt = stmt.where(AssessmentInvitation.assessment_id == uuid.UUID(str(params["assessment_id"])))
    for cond in _demand_filter(filters, Application.demand_id):
        stmt = stmt.where(cond)
    seen: dict[tuple[Any, Any], Point] = {}
    for cid, did, result, score in (await db.execute(stmt)).tuples():
        seen.setdefault(
            (cid, did), Point(cid, did, result == "pass", Decimal(score) if score is not None else None)
        )
    return list(seen.values())


SELECTORS = {
    "stage_transition": stage_transition,
    "ai_match_threshold": ai_match_threshold,
    "ai_screening_pass": ai_screening_pass,
    "assessment_pass": assessment_pass,
}

# Epic 65 (FR-323, C-16): the Phase 4 agent selection points, implemented in ``aedt_p4`` (projections only).
from app.services.fairness.aedt_p4 import DEFINITIONS_P4, SELECTORS_P4  # noqa: E402

SELECTORS.update(SELECTORS_P4)
DEFINITIONS.update(DEFINITIONS_P4)


async def collect(
    db: AsyncSession,
    selection_point: str,
    params: dict[str, Any],
    date_from: date,
    date_to: date,
    filters: dict[str, Any],
) -> list[Point]:
    lo, hi = _bounds(date_from, date_to)
    return await SELECTORS[selection_point](db, params, lo, hi, filters)


P3_LABELS = {
    "stage_transition": "Stage transition",
    "ai_match_threshold": "AI Match threshold",
    "ai_screening_pass": "AI Screening pass",
    "assessment_pass": "Assessment pass",
}


async def catalogue(db: AsyncSession) -> list[dict[str, Any]]:
    """Epic 65: every selection point with its definition; Phase 4 points are ``available`` only when their source
    projections exist (hidden from the run-request form otherwise)."""
    from app.services.fairness.aedt_p4 import LABELS_P4, point_available

    labels = {**P3_LABELS, **LABELS_P4}
    out: list[dict[str, Any]] = []
    for key in SELECTORS:
        available = await point_available(db, key) if key in LABELS_P4 else True
        out.append(
            {
                "key": key,
                "label": labels.get(key, key),
                "definition": DEFINITIONS.get(key, ""),
                "available": available,
            }
        )
    return out
