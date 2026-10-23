# ruff: file-ignore[line-too-long]
"""Assessments erasure handler ``assessments.attempts`` (order 45), Epic 40 A13 (FR-258, AD-34).

On Candidate erasure: attempts and their children (answers, proctoring signals and annotations, review scores) and
Submission shares are deleted ("attempts follow the Candidate's retention"); each invitation keeps only
``{assessment_version_id, status, result, score, scored_at}`` with ``candidate_id`` / application / context nulled so
percentile cohorts stay valid and anonymous; the attempt link is revoked and the report reference tombstoned.
``export`` returns the invitations, scores and the Candidate's own answers -- never answer keys, rubrics or reviewer
notes.
"""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.models.assessments import (
    Assessment,
    AssessmentInvitation,
    Attempt,
    AttemptAnswer,
    ProctoringAnnotation,
    ProctoringSignal,
    ReviewScore,
    SubmissionAssessmentShare,
)
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class AssessmentsHandler:
    name = "assessments.attempts"
    order = 45
    tables = (
        "assessment_invitations", "attempts", "attempt_answers", "proctoring_signals", "proctoring_annotations",
        "review_scores", "submission_assessment_shares",
    )  # fmt: skip

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        invs = list(
            (
                await db.execute(
                    select(AssessmentInvitation).where(AssessmentInvitation.candidate_id.in_(ids))
                )
            ).scalars()
        )
        inv_ids = [i.id for i in invs]
        counts = {"invitations": len(invs), "attempts": 0, "shares": 0}
        if not inv_ids:
            return counts
        attempt_ids = list(
            (await db.execute(select(Attempt.id).where(Attempt.invitation_id.in_(inv_ids)))).scalars()
        )
        if attempt_ids:
            answer_ids = select(AttemptAnswer.id).where(AttemptAnswer.attempt_id.in_(attempt_ids))
            await db.execute(delete(ReviewScore).where(ReviewScore.answer_id.in_(answer_ids)))
            await db.execute(
                delete(ProctoringAnnotation).where(ProctoringAnnotation.attempt_id.in_(attempt_ids))
            )
            await db.execute(delete(ProctoringSignal).where(ProctoringSignal.attempt_id.in_(attempt_ids)))
            await db.execute(delete(AttemptAnswer).where(AttemptAnswer.attempt_id.in_(attempt_ids)))
            res = await db.execute(delete(Attempt).where(Attempt.id.in_(attempt_ids)))
            counts["attempts"] = int(getattr(res, "rowcount", 0) or 0)
        res = await db.execute(
            delete(SubmissionAssessmentShare).where(SubmissionAssessmentShare.invitation_id.in_(inv_ids))
        )
        counts["shares"] = int(getattr(res, "rowcount", 0) or 0)
        for inv in invs:
            await secure_links.revoke_for_subject(db, "assessment_invitation", inv.id)
            # Anonymous result row: assessment_version_id, status, result, score, scored_at (percentile cohorts).
            inv.candidate_id = None
            inv.application_id = None
            inv.context_kind = "direct"
            inv.context_id = None
            inv.idempotency_key = None
            inv.secure_link_id = None
            inv.consent_id = None
            inv.provider_ref = None
            inv.report_url = None
            inv.report_file_id = None  # tombstoned reference (the file itself follows the documents handler)
            inv.points = None
            inv.max_points = None
            inv.section_scores = None
            inv.skill_scores = None
            inv.percentile = None
            inv.decision_note = None
            inv.cancel_reason = None
            inv.decided_by = None
            inv.invited_by_id = None
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = (
            await db.execute(
                select(AssessmentInvitation, Assessment.name)
                .join(Assessment, Assessment.id == AssessmentInvitation.assessment_id)
                .where(AssessmentInvitation.candidate_id == ctx.candidate_id)
                .order_by(AssessmentInvitation.created_at)
            )
        ).all()
        if not rows:
            return None
        out = []
        for inv, name in rows:
            answers = (
                await db.execute(
                    select(AttemptAnswer.position, AttemptAnswer.response, AttemptAnswer.final_score)
                    .join(Attempt, Attempt.id == AttemptAnswer.attempt_id)
                    .where(Attempt.invitation_id == inv.id)
                    .order_by(AttemptAnswer.position)
                )
            ).all()
            out.append(
                {
                    "assessment": name, "status": inv.status, "result": inv.result, "score": inv.score,
                    "percentile": inv.percentile, "invited_at": inv.created_at, "scored_at": inv.scored_at,
                    "section_scores": inv.section_scores, "skill_scores": inv.skill_scores,
                    "answers": [{"position": p, "response": r, "score": s} for p, r, s in answers],
                }
            )  # fmt: skip
        return {"assessments": out}

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        findings: list[str] = []
        if (
            await db.execute(
                select(AssessmentInvitation.id).where(AssessmentInvitation.candidate_id.in_(ids))
            )
        ).first() is not None:
            findings.append("assessment invitations still name the candidate")
        return findings


register_erasure_handler(AssessmentsHandler())
