# ruff: file-ignore[line-too-long, unused-function-argument]
"""Review queue (Epic 40 A7, FR-205).

* ``GET /assessment-reviews?status=pending`` lists answers waiting for a human (free-form / essay, low-confidence
  short answers, anything scoring could not read).
* ``review`` appends a ``review_scores`` row (append-only) and sets ``final_score``; when every answer of the attempt
  is scored the invitation moves ``completed -> reviewed`` (system) and the result is finalised.
* ``ai_suggestion`` (feature ``assessment_review``) is **advisory**: it is returned to the reviewer and never written
  to ``final_score``; a human submits the review. AI off -> 409 ``ai_disabled``.

Never reads proctoring signals (invariant, A8).
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import require_human
from app.core.context import RequestContext
from app.models.assessments import (
    Assessment,
    AssessmentInvitation,
    AssessmentItemVersion,
    Attempt,
    AttemptAnswer,
    ReviewScore,
)
from app.schemas.assessments import AiSuggestionModel, AiSuggestionOut, ReviewEntry, ReviewIn, ReviewItemOut
from app.services.assessments import results
from app.services.assessments.common import actor_of, conflict, invalid, not_found, now, user_id
from app.services.assessments.delivery import to_original
from app.services.assessments.invitations import candidate_names

AI_FEATURE = "assessment_review"
PROMPT_ID = "assessment_review_suggest"


async def _outs(db: AsyncSession, answers: list[AttemptAnswer]) -> list[ReviewItemOut]:
    if not answers:
        return []
    attempts = {
        a.id: a
        for a in (
            await db.execute(select(Attempt).where(Attempt.id.in_({x.attempt_id for x in answers})))
        ).scalars()
    }
    invs = {
        i.id: i
        for i in (
            await db.execute(
                select(AssessmentInvitation).where(
                    AssessmentInvitation.id.in_({a.invitation_id for a in attempts.values()})
                )
            )
        ).scalars()
    }
    names = dict(
        (
            await db.execute(
                select(Assessment.id, Assessment.name).where(
                    Assessment.id.in_({i.assessment_id for i in invs.values()})
                )
            )
        )
        .tuples()
        .all()
    )
    cands = await candidate_names(db, [i.candidate_id for i in invs.values() if i.candidate_id])
    versions = {
        v.id: v
        for v in (
            await db.execute(
                select(AssessmentItemVersion).where(
                    AssessmentItemVersion.id.in_({x.item_version_id for x in answers})
                )
            )
        ).scalars()
    }
    reviews: dict[uuid.UUID, list[ReviewScore]] = {}
    for r in (
        await db.execute(
            select(ReviewScore)
            .where(ReviewScore.answer_id.in_([x.id for x in answers]))
            .order_by(ReviewScore.created_at)
        )
    ).scalars():
        reviews.setdefault(r.answer_id, []).append(r)
    out: list[ReviewItemOut] = []
    for a in answers:
        att = attempts[a.attempt_id]
        inv = invs[att.invitation_id]
        entry = results.plan_index(att).get(a.position, ("", {}))[1]
        v = versions.get(a.item_version_id)
        out.append(
            ReviewItemOut(
                answer_id=a.id, attempt_id=att.id, invitation_id=inv.id, assessment_name=names.get(inv.assessment_id, ""),
                candidate_name=cands.get(inv.candidate_id) if inv.candidate_id else None, position=a.position,
                section_key=a.section_key, kind=str(entry.get("kind") or ""), stem=v.stem if v else "", code=v.code if v else None,
                language=v.language if v else None, response=to_original(entry, a.response), max_points=a.max_points,
                auto_score=a.auto_score, auto_confidence=a.auto_confidence, needs_review=a.needs_review,
                final_score=a.final_score, scored_via=a.scored_via, rubric=v.rubric if v else None,
                reviews=[ReviewEntry(reviewer_id=r.reviewer_id, score=r.score, note=r.note, created_at=r.created_at) for r in reviews.get(a.id, [])],
            )
        )  # fmt: skip
    return out


async def queue(ctx: RequestContext, db: AsyncSession, status: str = "pending") -> list[ReviewItemOut]:
    stmt = (
        select(AttemptAnswer)
        .join(Attempt, Attempt.id == AttemptAnswer.attempt_id)
        .join(AssessmentInvitation, AssessmentInvitation.id == Attempt.invitation_id)
        .where(AttemptAnswer.needs_review.is_(True), AssessmentInvitation.candidate_id.is_not(None))
    )
    if status == "pending":
        stmt = stmt.where(AttemptAnswer.final_score.is_(None), AssessmentInvitation.status == "completed")
    else:
        stmt = stmt.where(AttemptAnswer.final_score.is_not(None))
    rows = list(
        (await db.execute(stmt.order_by(Attempt.submitted_at, AttemptAnswer.position).limit(500))).scalars()
    )
    return await _outs(db, rows)


async def attempt_answers(
    ctx: RequestContext, db: AsyncSession, attempt_id: uuid.UUID
) -> list[ReviewItemOut]:
    att = await db.get(Attempt, attempt_id)
    if att is None:
        raise not_found("Attempt")
    if att.status == "in_progress":
        raise conflict("attempt_in_progress", "Answers are visible after the attempt is submitted")
    rows = list(
        (
            await db.execute(
                select(AttemptAnswer)
                .where(AttemptAnswer.attempt_id == att.id)
                .order_by(AttemptAnswer.position)
            )
        ).scalars()
    )
    return await _outs(db, rows)


async def _answer(
    db: AsyncSession, answer_id: uuid.UUID, *, for_update: bool = False
) -> tuple[AttemptAnswer, Attempt, AssessmentInvitation]:
    stmt = select(AttemptAnswer).where(AttemptAnswer.id == answer_id)
    if for_update:
        stmt = stmt.with_for_update()
    a = (await db.execute(stmt)).scalar_one_or_none()
    if a is None:
        raise not_found("Answer")
    att = await db.get(Attempt, a.attempt_id)
    inv = await db.get(AssessmentInvitation, att.invitation_id, with_for_update=for_update) if att else None
    if att is None or inv is None or inv.candidate_id is None:
        raise not_found("Answer")
    return a, att, inv


async def review(
    ctx: RequestContext, db: AsyncSession, answer_id: uuid.UUID, data: ReviewIn
) -> ReviewItemOut:
    require_human(actor_of(ctx))
    a, att, inv = await _answer(db, answer_id, for_update=True)
    if att.status == "in_progress":
        raise conflict("attempt_in_progress", "The attempt is still in progress")
    if inv.status not in ("completed", "reviewed"):
        raise conflict("review_closed", "This result is already decided", status=inv.status)
    if data.score > a.max_points:
        raise invalid("score_above_max", f"The score cannot exceed {a.max_points}", "score")
    if data.rubric_marks:
        v = await db.get(AssessmentItemVersion, a.item_version_id)
        criteria = (
            {c["key"]: Decimal(str(c["max_points"])) for c in ((v.rubric or {}).get("criteria") or [])}
            if v
            else {}
        )
        for key, mark in data.rubric_marks.items():
            if key not in criteria or mark > criteria[key]:
                raise invalid(
                    "invalid_rubric_mark",
                    f"Rubric mark '{key}' is unknown or above its maximum",
                    "rubric_marks",
                )
    db.add(
        ReviewScore(
            id=uuid.uuid4(), tenant_id=a.tenant_id, answer_id=a.id, reviewer_id=user_id(ctx), score=data.score,
            rubric_marks={k: str(v) for k, v in (data.rubric_marks or {}).items()} or None, note=data.note, created_at=now(),
        )
    )  # fmt: skip
    a.final_score = data.score
    a.scored_via = "review"
    a.needs_review = True  # stays flagged as a reviewed answer (final_score set)
    await db.flush()
    if inv.status == "completed":
        await results.complete_review_if_done(db, inv, att)
    elif inv.status == "reviewed":
        await results.finalize(db, inv, source="builtin", computed=await results.compute(db, att))
    return (await _outs(db, [a]))[0]


async def ai_suggestion(ctx: RequestContext, db: AsyncSession, answer_id: uuid.UUID) -> AiSuggestionOut:
    from app.services.ai import gateway

    a, att, inv = await _answer(db, answer_id)
    if not await gateway.is_feature_enabled(db, AI_FEATURE):
        raise conflict("ai_disabled", "AI suggestions are turned off for this workspace")
    v = await db.get(AssessmentItemVersion, a.item_version_id)
    entry = results.plan_index(att).get(a.position, ("", {}))[1]
    response: dict[str, Any] = to_original(entry, a.response) or {}
    answer_text = str(response.get("text") or response)[:20_000]
    import json

    result = await gateway.run(
        ctx, db, AI_FEATURE, PROMPT_ID,
        inputs={"stem": (v.stem if v else "")[:8000], "rubric": json.dumps((v.rubric if v else None) or {}), "points": str(a.max_points), "code": (v.code or "") if v else ""},
        untrusted={"answer": answer_text}, output_schema=AiSuggestionModel, subject=("candidate", inv.candidate_id),  # type: ignore[arg-type]
    )  # fmt: skip
    if not result.ok or result.data is None:
        raise conflict("ai_unavailable", "No AI suggestion could be produced", error=result.error_code)
    data: AiSuggestionModel = result.data  # type: ignore[assignment]
    suggested = min(max(data.suggested_score, Decimal(0)), a.max_points).quantize(Decimal("0.01"))
    return AiSuggestionOut(
        suggested_score=suggested, rationale=data.rationale[:400], ai_action_id=result.action_id
    )


__all__ = ["AI_FEATURE", "PROMPT_ID", "ai_suggestion", "attempt_answers", "queue", "review"]
