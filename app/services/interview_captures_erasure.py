# ruff: noqa: E501
"""Interview Assistant erasure handler ``interview_captures.records`` (order 69), Epic 60 (FR-326, AD-34).

On Candidate erasure (merged ids included): the outputs, draft scorecards, suggestion decisions and per-interview
acknowledgements of the Candidate's interviews are deleted and the hub rows marked ``erased`` (recording files are
purged by ``recordings.files``, order 64; transcripts by ``transcripts.records``, order 63). Interviewers' standing
acknowledgements are not the Candidate's data and stay. A top-level module so ``privacy`` never imports the Phase 4
domain.
"""

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.interview_captures import (
    InterviewCapture,
    InterviewCaptureAck,
    InterviewCaptureOutput,
    InterviewScorecardDraft,
    ScorecardSuggestionDecision,
)
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class InterviewCaptureRecordsHandler:
    name = "interview_captures.records"
    order = 69
    tables = (
        "interview_capture_outputs", "interview_scorecard_drafts", "scorecard_suggestion_decisions",
        "interview_capture_acks",
    )  # fmt: skip

    async def _captures(self, db: AsyncSession, ctx: ErasureContext) -> list[InterviewCapture]:
        ids = await subject_ids(db, ctx.candidate_id)
        keep = ctx.retained.get("interview_captures", set())
        rows = (
            await db.execute(select(InterviewCapture).where(InterviewCapture.candidate_id.in_(ids)))
        ).scalars()
        return [c for c in rows if c.id not in keep]

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"outputs": 0, "drafts": 0, "acks": 0}
        captures = await self._captures(db, ctx)
        capture_ids = [c.id for c in captures]
        interview_ids = [c.interview_id for c in captures]
        if not capture_ids:
            return {"outputs": 0, "drafts": 0, "acks": 0}
        draft_ids = list(
            (
                await db.execute(
                    select(InterviewScorecardDraft.id).where(
                        InterviewScorecardDraft.capture_id.in_(capture_ids)
                    )
                )
            ).scalars()
        )
        if draft_ids:
            await db.execute(
                delete(ScorecardSuggestionDecision).where(ScorecardSuggestionDecision.draft_id.in_(draft_ids))
            )
        drafts = await db.execute(
            delete(InterviewScorecardDraft).where(InterviewScorecardDraft.capture_id.in_(capture_ids))
        )
        outputs = await db.execute(
            delete(InterviewCaptureOutput).where(InterviewCaptureOutput.capture_id.in_(capture_ids))
        )
        acks = await db.execute(
            delete(InterviewCaptureAck).where(
                InterviewCaptureAck.scope == "interview", InterviewCaptureAck.interview_id.in_(interview_ids)
            )
        )
        await db.execute(
            update(InterviewCapture)
            .where(InterviewCapture.id.in_(capture_ids))
            .values(
                status="erased",
                participant_acks=[],
                blocked_reasons=[],
                speakers=[],
                confirmed_country=None,
                confirmed_region=None,
            )
        )
        await db.flush()
        return {
            "outputs": int(getattr(outputs, "rowcount", 0) or 0),
            "drafts": int(getattr(drafts, "rowcount", 0) or 0),
            "acks": int(getattr(acks, "rowcount", 0) or 0),
        }

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        rows = (
            await db.execute(
                select(InterviewCaptureOutput).where(
                    InterviewCaptureOutput.candidate_id == ctx.candidate_id,
                    InterviewCaptureOutput.status == "ready",
                )
            )
        ).scalars()
        items = [
            {
                "interview_id": str(r.interview_id), "created_at": r.created_at, "overall_summary": r.overall_summary,
                "summary_by_competency": r.summary_by_competency, "questions": r.qa_items, "ai_generated": True,
            }
            for r in rows
        ]  # fmt: skip
        return {"interview_assistant_outputs": items} if items else None

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        capture_ids = [c.id for c in await self._captures(db, ctx)]
        if not capture_ids:
            return []
        left = (
            await db.execute(
                select(InterviewCaptureOutput.id)
                .where(InterviewCaptureOutput.capture_id.in_(capture_ids))
                .limit(1)
            )
        ).first()
        drafts = (
            await db.execute(
                select(InterviewScorecardDraft.id)
                .where(InterviewScorecardDraft.capture_id.in_(capture_ids))
                .limit(1)
            )
        ).first()
        out = []
        if left is not None:
            out.append("interview capture outputs remain")
        if drafts is not None:
            out.append("interview draft scorecards remain")
        return out


register_erasure_handler(InterviewCaptureRecordsHandler())
