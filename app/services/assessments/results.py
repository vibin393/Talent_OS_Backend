# ruff: noqa: E501
"""Attempt scoring and results (Epic 40 A7, FR-205).

* ``score_attempt`` (on submit / auto-submit): every answer is scored by the pure ``scoring.score_answer`` (responses
  mapped back to the item's own ids first); objective answers get ``final_score`` at once, the rest wait in the
  review queue.
* ``finalize`` (at ``reviewed``): ``points``, ``max_points``, ``score`` = points / max x 100 (the hub percentage),
  ``section_scores``, ``skill_scores`` (points-weighted percentage per ``SkillKey`` on items; free tags are display
  only) and ``percentile`` (percent rank among ``reviewed|passed|failed`` invitations of the same assessment,
  snapshotted). Emits ``AssessmentScored(skill_scores=((key, proficiency), ...))`` -- the proficiency is the pure
  map >= 90 -> 5, >= 75 -> 4, >= 60 -> 3, >= 40 -> 2, else 1 -- and applies non-gating auto-pass (A5).

Never reads proctoring signals (invariant, A8).
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import emit
from app.events.assessments import AssessmentScored
from app.models.assessments import (
    Assessment,
    AssessmentInvitation,
    AssessmentItem,
    AssessmentItemVersion,
    Attempt,
    AttemptAnswer,
)
from app.services.assessments import lifecycle, scoring
from app.services.assessments import settings as settings_mod
from app.services.assessments.common import now
from app.services.assessments.delivery import to_original
from app.services.assessments.events import AssessmentResultReady, AssessmentReviewNeeded

ZERO = Decimal(0)


def plan_index(att: Attempt) -> dict[int, tuple[str, dict[str, Any]]]:
    """Position -> (section key, plan entry)."""
    out: dict[int, tuple[str, dict[str, Any]]] = {}
    for section in att.section_plan or []:
        for entry in section.get("items") or []:
            out[int(entry["position"])] = (section["key"], entry)
    return out


async def score_attempt(db: AsyncSession, inv: AssessmentInvitation, att: Attempt) -> int:
    """Auto-score every answer; returns the number of answers waiting for a human review."""
    s = await settings_mod.current(db)
    plan = plan_index(att)
    answers = list(
        (await db.execute(select(AttemptAnswer).where(AttemptAnswer.attempt_id == att.id))).scalars()
    )
    versions = {
        v.id: v
        for v in (
            await db.execute(
                select(AssessmentItemVersion).where(
                    AssessmentItemVersion.id.in_([a.item_version_id for a in answers])
                )
            )
        ).scalars()
    }
    total = ZERO
    max_total = ZERO
    pending = 0
    for a in answers:
        entry = plan.get(a.position, ("", {}))[1]
        kind = str(entry.get("kind") or "")
        v = versions.get(a.item_version_id)
        snapshot = {
            "points": a.max_points,
            "answer_key": v.answer_key if v else None,
            "rubric": v.rubric if v else None,
        }
        response = to_original(entry, a.response)
        if kind in scoring.REVIEW_KINDS and not (response or {}).get("text", "").strip():
            res = scoring.ScoreResult(ZERO, Decimal(1), False)  # blank free-form answer: nothing to review
        else:
            res = scoring.score_answer(
                kind, snapshot, response, partial_credit=s.multi_choice_partial_credit,
                confidence_threshold=s.short_answer_confidence_threshold,
            )  # fmt: skip
        a.auto_score = res.points
        a.auto_confidence = res.confidence
        a.needs_review = res.needs_review
        if res.needs_review:
            pending += 1
        else:
            a.final_score = res.points
            a.scored_via = "auto"
        total += res.points
        max_total += a.max_points
    att.auto_score_total = total
    att.max_points = max_total
    await db.flush()
    if pending:
        emit(
            db,
            AssessmentReviewNeeded(
                tenant_id=inv.tenant_id, invitation_id=inv.id,
                inviter_id=inv.invited_by_id if inv.invited_by_kind == "user" else None, pending=pending,
            ),
        )  # fmt: skip
    return pending


async def _item_skills(db: AsyncSession, version_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    if not version_ids:
        return {}
    rows = await db.execute(
        select(AssessmentItemVersion.id, AssessmentItem.skill_keys)
        .join(AssessmentItem, AssessmentItem.id == AssessmentItemVersion.item_id)
        .where(AssessmentItemVersion.id.in_(version_ids))
    )
    return {vid: list(keys or []) for vid, keys in rows.tuples()}


async def compute(db: AsyncSession, att: Attempt) -> dict[str, Any]:
    plan = plan_index(att)
    titles = {s["key"]: s.get("title", s["key"]) for s in att.section_plan or []}
    answers = list(
        (await db.execute(select(AttemptAnswer).where(AttemptAnswer.attempt_id == att.id))).scalars()
    )
    skills_of = await _item_skills(db, [a.item_version_id for a in answers])
    points = ZERO
    max_points = ZERO
    sections: dict[str, list[Decimal]] = {}
    skills: dict[str, list[Decimal]] = {}
    for a in answers:
        got = a.final_score if a.final_score is not None else ZERO
        points += got
        max_points += a.max_points
        key = plan.get(a.position, (a.section_key, {}))[0] or a.section_key
        sec = sections.setdefault(key, [ZERO, ZERO])
        sec[0] += got
        sec[1] += a.max_points
        for sk in skills_of.get(a.item_version_id, []):
            acc = skills.setdefault(sk, [ZERO, ZERO])
            acc[0] += got
            acc[1] += a.max_points
    section_scores = {
        k: {"title": titles.get(k, k), "pct": str(scoring.pct(p, m)), "points": str(p), "max_points": str(m)}
        for k, (p, m) in sections.items()
    }
    skill_scores: dict[str, Any] = {}
    for k, (p, m) in sorted(skills.items()):
        pc = scoring.pct(p, m)
        skill_scores[k] = {"pct": str(pc), "proficiency": scoring.proficiency_for(pc), "label": None}
    return {
        "points": points, "max_points": max_points, "score": scoring.pct(points, max_points),
        "section_scores": section_scores, "skill_scores": skill_scores,
    }  # fmt: skip


async def percentile(db: AsyncSession, inv: AssessmentInvitation) -> Decimal | None:
    if inv.score is None:
        return None
    cohort = (
        AssessmentInvitation.assessment_id == inv.assessment_id,
        AssessmentInvitation.status.in_(("reviewed", "passed", "failed")),
        AssessmentInvitation.score.is_not(None),
    )
    n = (await db.execute(select(func.count()).where(*cohort))).scalar_one()
    if n <= 1:
        return Decimal("0.00") if n == 1 else None  # percent_rank of a single row
    below = (
        await db.execute(select(func.count()).where(*cohort, AssessmentInvitation.score < inv.score))
    ).scalar_one()
    return (Decimal(below) / Decimal(n - 1) * 100).quantize(Decimal("0.01"))


async def finalize(
    db: AsyncSession,
    inv: AssessmentInvitation,
    *,
    source: str,
    computed: dict[str, Any] | None = None,
    evidence: tuple[tuple[str, int], ...] | None = None,
) -> None:
    """Write the result onto the invitation (already ``reviewed``), snapshot the percentile, emit
    ``AssessmentScored`` and apply non-gating auto-pass."""
    if computed is not None:
        inv.points = computed.get("points")
        inv.max_points = computed.get("max_points")
        inv.score = computed.get("score")
        inv.section_scores = computed.get("section_scores") or {}
        inv.skill_scores = computed.get("skill_scores") or {}
    inv.scored_at = now()
    await db.flush()
    inv.percentile = await percentile(db, inv)
    await db.flush()
    if evidence is None:
        evidence = tuple(
            (k, int(v["proficiency"]))
            for k, v in sorted((inv.skill_scores or {}).items())
            if isinstance(v, dict) and v.get("proficiency") and _is_skill_key(k)
        )
    if inv.candidate_id is not None:
        emit(
            db,
            AssessmentScored(
                tenant_id=inv.tenant_id, invitation_id=inv.id, assessment_id=inv.assessment_id, candidate_id=inv.candidate_id,
                application_id=inv.application_id, score=str(inv.score) if inv.score is not None else None,
                skill_scores=evidence, source=source,
            ),
        )  # fmt: skip
        emit(
            db,
            AssessmentResultReady(
                tenant_id=inv.tenant_id, invitation_id=inv.id, application_id=inv.application_id, candidate_id=inv.candidate_id,
                score=str(inv.score) if inv.score is not None else None, source=source,
            ),
        )  # fmt: skip
    await maybe_auto_pass(db, inv)


def _is_skill_key(key: str) -> bool:
    from app.core.skill_refs import SkillKey

    try:
        SkillKey.parse(key)
    except ValueError:
        return False
    return True


async def maybe_auto_pass(db: AsyncSession, inv: AssessmentInvitation) -> bool:
    if inv.gating or inv.status != "reviewed":
        return False
    s = await settings_mod.current(db)
    assessment = await db.get(Assessment, inv.assessment_id)
    threshold = assessment.auto_pass_threshold_pct if assessment else None
    kw = {"auto_pass_allowed": s.allow_auto_pass_non_gating, "score": inv.score, "threshold": threshold}
    if not lifecycle.allowed("reviewed", "passed", "system", gating=inv.gating, **kw):  # type: ignore[arg-type]
        return False
    await lifecycle.move(db, inv, "passed", "system", result="pass", **kw)
    return True


async def complete_review_if_done(
    db: AsyncSession, inv: AssessmentInvitation, att: Attempt, actor_kind: str = "system"
) -> bool:
    """When no answer waits for a review: ``completed -> reviewed`` and finalize (built-in)."""
    waiting = (
        await db.execute(
            select(func.count()).where(
                AttemptAnswer.attempt_id == att.id,
                AttemptAnswer.needs_review.is_(True),
                AttemptAnswer.final_score.is_(None),
            )
        )
    ).scalar_one()
    if waiting or inv.status != "completed":
        return False
    await lifecycle.move(db, inv, "reviewed", actor_kind)
    await finalize(db, inv, source="builtin", computed=await compute(db, att))
    return True


__all__ = [
    "complete_review_if_done",
    "compute",
    "finalize",
    "maybe_auto_pass",
    "percentile",
    "plan_index",
    "score_attempt",
]
