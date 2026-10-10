"""Interview-domain erasure handler (Epic 12 registry, FR-68): ``interviews.scorecards`` (order 55).

For every interview of the erased candidate: interview notes and cancellation reasons are cleared, the
candidate participant's name / email are replaced by a placeholder, scorecard notes / answers / summaries and
stage-outcome comments are cleared. Rows, times, statuses, ratings and recommendations stay (the fact that
interviews happened remains, without personal text).
"""

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.interviews import Interview, InterviewParticipant, InterviewStageOutcome, Scorecard
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED_DOMAIN = "erased.invalid"


class InterviewsHandler:
    name = "interviews.scorecards"
    order = 55
    tables = ("interviews", "interview_participants", "scorecards", "interview_stage_outcomes")

    async def _interviews(self, db: AsyncSession, ctx: ErasureContext) -> list[Interview]:
        ids = await subject_ids(db, ctx.candidate_id)
        return list((await db.execute(select(Interview).where(Interview.candidate_id.in_(ids)))).scalars())

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        rows = await self._interviews(db, ctx)
        ids = [r.id for r in rows]
        for r in rows:
            r.notes = None
            r.cancelled_reason = None
        participants = cards = outcomes = 0
        if ids:
            for p in (
                await db.execute(
                    select(InterviewParticipant).where(
                        InterviewParticipant.interview_id.in_(ids), InterviewParticipant.kind == "candidate"
                    )
                )
            ).scalars():
                p.name = None
                p.email = f"candidate-{p.id.hex[:12]}@{ERASED_DOMAIN}"
                participants += 1
            for c in (await db.execute(select(Scorecard).where(Scorecard.interview_id.in_(ids)))).scalars():
                c.summary = None
                c.answers = []
                c.ratings = [{**r, "note": None} for r in (c.ratings or [])]
                cards += 1
            app_ids = list({r.application_id for r in rows})
            for o in (
                await db.execute(
                    select(InterviewStageOutcome).where(InterviewStageOutcome.application_id.in_(app_ids))
                )
            ).scalars():
                o.comment = None
                outcomes += 1
        await db.flush()
        return {
            "interviews": len(rows),
            "participants": participants,
            "scorecards": cards,
            "outcomes": outcomes,
        }

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = (
            await db.execute(select(Interview).where(Interview.candidate_id == ctx.candidate_id))
        ).scalars()
        return {
            "interviews": [
                {
                    "human_id": r.human_id,
                    "stage_name": r.stage_name,
                    "starts_at": r.starts_at,
                    "ends_at": r.ends_at,
                    "mode": r.mode,
                    "status": r.status,
                }
                for r in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        rows = await self._interviews(db, ctx)
        ids = [r.id for r in rows]
        findings = [f"interview {r.id} still holds notes" for r in rows if r.notes or r.cancelled_reason]
        if ids:
            left = await db.execute(
                select(InterviewParticipant.id).where(
                    InterviewParticipant.interview_id.in_(ids),
                    InterviewParticipant.kind == "candidate",
                    or_(
                        InterviewParticipant.name.is_not(None),
                        ~InterviewParticipant.email.like(f"%@{ERASED_DOMAIN}"),
                    ),
                )
            )
            findings += [f"candidate participant {pid} not anonymised" for pid in left.scalars()]
            cards = await db.execute(
                select(Scorecard.id).where(Scorecard.interview_id.in_(ids), Scorecard.summary.is_not(None))
            )
            findings += [f"scorecard {cid} still holds a summary" for cid in cards.scalars()]
        return findings


register_erasure_handler(InterviewsHandler())
