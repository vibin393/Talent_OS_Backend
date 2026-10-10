# ruff: noqa: E501
"""Who may see the assistant's outputs (Epic 60, story 60.3).

* A draft is visible only to the interviewer it belongs to (``drafts.mine``).
* The summary / Q&A outputs are visible to interviewers who own a draft for the capture and -- the P1 blind rule --
  to whoever may see the submitted scorecards (their own scorecard is submitted, or they hold ``interview:outcome``
  and are not a required participant). Everyone else -> 404.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.interview_captures import InterviewScorecardDraft
from app.models.skills_graph import SkillSuggestion
from app.schemas.interview_captures import CompetencySummaryOut, OutputsOut, QaItemOut
from app.services.interview_captures import common, consent, outputs


async def may_see_outputs(
    ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID, capture_id: uuid.UUID
) -> bool:
    from app.services import interviews_service

    user_id = common.principal_of(ctx).id
    owns = (
        await db.execute(
            select(InterviewScorecardDraft.id).where(
                InterviewScorecardDraft.capture_id == capture_id,
                InterviewScorecardDraft.user_id == user_id,
                InterviewScorecardDraft.status != "superseded",
            )
        )
    ).first()
    if owns is not None:
        return True
    listing = await interviews_service.list_scorecards(ctx, db, interview_id)
    return not listing.hidden


async def get_outputs(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> OutputsOut:
    await consent.readable_interview(ctx, db, interview_id)
    capture = await common.capture_for_interview(db, interview_id)
    if capture is None:
        raise NotFound("Outputs not found")
    row = await outputs.current(db, capture.id)
    if row is None or not await may_see_outputs(ctx, db, interview_id, capture.id):
        raise NotFound("Outputs not found")
    suggestions = (
        await db.execute(
            select(func.count())
            .select_from(SkillSuggestion)
            .where(SkillSuggestion.source_ref == f"interview_capture:{capture.id}")
        )
    ).scalar_one()
    return OutputsOut(
        capture_id=capture.id, interview_id=interview_id, candidate_id=capture.candidate_id, status=row.status,
        summary_by_competency=[CompetencySummaryOut(**s) for s in row.summary_by_competency or []],
        qa_items=[QaItemOut(**q) for q in row.qa_items or []], overall_summary=row.overall_summary,
        ai_generated=row.ai_generated, skill_suggestion_count=int(suggestions or 0), created_at=row.created_at,
    )  # fmt: skip


__all__ = ["get_outputs", "may_see_outputs"]
