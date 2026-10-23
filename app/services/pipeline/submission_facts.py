# ruff: file-ignore[line-too-long]
"""Read-only submission facts (Epic 61 A2, additive; no behaviour change).

The P1 rules that the Submission dialog applies, extracted from ``pipeline.submissions`` (a human-only module that
Phase 4 code may not import) so the Submission Agent's pre-send checks and the dialog share one implementation:

* ``prior_submission(db, candidate_id, client_id, window_days)`` -- the latest ``sent`` submission of the candidate to
  the client inside the window (the duplicate rule), else None;
* ``counted_submissions(db, demand_id)`` -- submissions counting toward the demand's submission limit;
* ``client_first_submitter(db, client_id, candidate_id)`` -- the candidate/client "first" row (who submitted first);
* ``get_pipeline_settings(db)`` -- the tenant pipeline settings row (consent mode, duplicate window, minimum margin).
"""

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pipeline import CandidateClientFirst, PipelineSettings, Submission
from app.services.pipeline.common import now, settings_row

COUNTED_STATUSES = ("pending_approval", "approved", "sent")


async def counted_submissions(db: AsyncSession, demand_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(Submission)
                .where(Submission.demand_id == demand_id, Submission.status.in_(COUNTED_STATUSES))
            )
        ).scalar_one()
    )


async def prior_submission(
    db: AsyncSession, candidate_id: uuid.UUID, client_id: uuid.UUID, window_days: int
) -> Submission | None:
    if window_days <= 0:
        return None
    since = now() - timedelta(days=window_days)
    return (
        await db.execute(
            select(Submission)
            .where(
                Submission.candidate_id == candidate_id,
                Submission.client_id == client_id,
                Submission.status == "sent",
                Submission.sent_at >= since,
            )
            .order_by(Submission.sent_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def client_first_submitter(
    db: AsyncSession, client_id: uuid.UUID, candidate_id: uuid.UUID
) -> CandidateClientFirst | None:
    return (
        await db.execute(
            select(CandidateClientFirst).where(
                CandidateClientFirst.client_id == client_id, CandidateClientFirst.candidate_id == candidate_id
            )
        )
    ).scalar_one_or_none()


async def get_pipeline_settings(db: AsyncSession) -> PipelineSettings:
    return await settings_row(db)


__all__ = [
    "COUNTED_STATUSES",
    "client_first_submitter",
    "counted_submissions",
    "get_pipeline_settings",
    "prior_submission",
]
