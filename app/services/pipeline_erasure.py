"""Pipeline-domain erasure handlers (Epic 12 registry, FR-68) plus retention / consent hooks.

* ``pipeline.screenings`` (40): clear answers, notes, AI summary and the questions snapshot.
* ``pipeline.submissions`` (41): clear email subject / body and note, prior-submission reasons,
  client-feedback comments and stage-history notes; delete the snapshot resume file from storage (row kept
  as a tombstone). Rows, rates and statuses stay (the submission fact remains).
* Retention: open applications (non-terminal stage) exclude a candidate; stage changes are activity.
* AI consent withdrawal: AI screening summaries are AI-derived data and are purged.
"""

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.models.files import StoredFile
from app.models.pipeline import (
    Application,
    ApplicationStageHistory,
    ClientFeedback,
    PipelineStage,
    Screening,
    Submission,
)
from app.services.candidates_erasure import subject_ids
from app.services.privacy.common import now
from app.services.privacy.registry import (
    ErasureContext,
    register_activity_signal,
    register_ai_derived_purger,
    register_erasure_handler,
    register_retention_exclusion,
)

ERASED = "[erased]"
TERMINAL = ("hired", "rejected", "withdrawn")


class ScreeningsHandler:
    name = "pipeline.screenings"
    order = 40
    tables = ("screenings",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        rows = list((await db.execute(select(Screening).where(Screening.candidate_id.in_(ids)))).scalars())
        for row in rows:
            row.answers = {}
            row.notes = None
            row.ai_summary = None
            row.questions = []
        await db.flush()
        return {"screenings": len(rows)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = (
            await db.execute(select(Screening).where(Screening.candidate_id == ctx.candidate_id))
        ).scalars()
        return {
            "screenings": [
                {
                    "kind": r.kind,
                    "status": r.status,
                    "outcome": r.outcome,
                    "questions": r.questions,
                    "answers": r.answers,
                    "ai_summary": r.ai_summary,
                    "created_at": r.created_at,
                }
                for r in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        left = await db.execute(
            select(Screening.id).where(
                Screening.candidate_id.in_(ids),
                or_(
                    Screening.notes.is_not(None),
                    Screening.ai_summary.is_not(None),
                    cast(Screening.answers, Text) != "{}",
                    cast(Screening.questions, Text) != "[]",
                ),
            )
        )
        return [f"screening {sid} still holds answers or notes" for sid in left.scalars()]


class SubmissionsHandler:
    name = "pipeline.submissions"
    order = 41
    tables = (
        "submissions",
        "client_feedback",
        "candidate_client_firsts",
        "applications",
        "application_stage_history",
    )

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        subs = list((await db.execute(select(Submission).where(Submission.candidate_id.in_(ids)))).scalars())
        file_ids = [s.resume_file_id for s in subs if s.resume_file_id]
        for sub in subs:
            sub.email_subject = None
            sub.email_body = None
            sub.note = None
            sub.prior_submission_reason = None
        app_ids = select(Application.id).where(Application.candidate_id.in_(ids))
        feedback = list(
            (
                await db.execute(select(ClientFeedback).where(ClientFeedback.application_id.in_(app_ids)))
            ).scalars()
        )
        for fb in feedback:
            fb.comment = None
        history = list(
            (
                await db.execute(
                    select(ApplicationStageHistory).where(
                        ApplicationStageHistory.application_id.in_(app_ids),
                        ApplicationStageHistory.note.is_not(None),
                    )
                )
            ).scalars()
        )
        for h in history:
            h.note = None
        deleted = 0
        at = now()
        backend = storage.get_storage()
        if file_ids:
            for file in (await db.execute(select(StoredFile).where(StoredFile.id.in_(file_ids)))).scalars():
                if file.deleted_at is None:
                    try:
                        await backend.delete(file.storage_key)
                    except FileNotFoundError:
                        pass
                    except Exception:
                        logger.exception("Could not delete submission resume {} from storage", file.id)
                        raise
                    file.deleted_at = at
                    deleted += 1
                file.original_name = ERASED
        await db.flush()
        return {
            "submissions": len(subs),
            "client_feedback": len(feedback),
            "stage_notes": len(history),
            "files_deleted": deleted,
        }

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        subs = (
            await db.execute(select(Submission).where(Submission.candidate_id == ctx.candidate_id))
        ).scalars()
        apps = (
            await db.execute(select(Application).where(Application.candidate_id == ctx.candidate_id))
        ).scalars()
        return {
            "applications": [
                {
                    "human_id": a.human_id,
                    "demand_id": a.demand_id,
                    "source": a.source,
                    "created_at": a.created_at,
                }
                for a in apps
            ],
            "submissions": [
                {"human_id": s.human_id, "client_id": s.client_id, "status": s.status, "sent_at": s.sent_at}
                for s in subs
            ],
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        findings: list[str] = []
        left = await db.execute(
            select(Submission.id, Submission.resume_file_id).where(Submission.candidate_id.in_(ids))
        )
        rows = left.all()
        text_left = await db.execute(
            select(Submission.id).where(
                Submission.candidate_id.in_(ids),
                or_(
                    Submission.email_subject.is_not(None),
                    Submission.email_body.is_not(None),
                    Submission.note.is_not(None),
                    Submission.prior_submission_reason.is_not(None),
                ),
            )
        )
        findings += [f"submission {sid} still holds email text" for sid in text_left.scalars()]
        file_ids = [f for _, f in rows if f]
        if file_ids:
            live = await db.execute(
                select(StoredFile.id).where(StoredFile.id.in_(file_ids), StoredFile.deleted_at.is_(None))
            )
            findings += [f"submission resume {fid} not deleted" for fid in live.scalars()]
        app_ids = select(Application.id).where(Application.candidate_id.in_(ids))
        comments = await db.execute(
            select(ClientFeedback.id).where(
                ClientFeedback.application_id.in_(app_ids), ClientFeedback.comment.is_not(None)
            )
        )
        findings += [f"client feedback {fid} still holds a comment" for fid in comments.scalars()]
        return findings


async def open_applications(db: AsyncSession, candidate_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Retention exclusion: candidates with an application in a non-terminal stage."""
    if not candidate_ids:
        return set()
    rows = await db.execute(
        select(Application.candidate_id)
        .join(PipelineStage, PipelineStage.id == Application.stage_id)
        .where(Application.candidate_id.in_(candidate_ids), PipelineStage.key.not_in(TERMINAL))
    )
    return set(rows.scalars())


async def stage_signal(db: AsyncSession, candidate_ids: list[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    if not candidate_ids:
        return {}
    rows = await db.execute(
        select(Application.candidate_id, func.max(Application.stage_entered_at))
        .where(Application.candidate_id.in_(candidate_ids))
        .group_by(Application.candidate_id)
    )
    return {cid: at for cid, at in rows.all() if at is not None}


async def purge_ai_summaries(db: AsyncSession, candidate_id: uuid.UUID) -> dict[str, int]:
    rows = list(
        (
            await db.execute(
                select(Screening).where(
                    Screening.candidate_id == candidate_id, Screening.ai_summary.is_not(None)
                )
            )
        ).scalars()
    )
    for row in rows:
        row.ai_summary = None
        row.summary_status = "purged"
    await db.flush()
    return {"screening_summaries": len(rows)}


async def find_ai_summaries(db: AsyncSession, candidate_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    ids = list(candidate_ids)
    if not ids:
        return set()
    rows = await db.execute(
        select(Screening.candidate_id).where(
            Screening.candidate_id.in_(ids), Screening.ai_summary.is_not(None)
        )
    )
    return set(rows.scalars())


register_erasure_handler(ScreeningsHandler())
register_erasure_handler(SubmissionsHandler())
register_retention_exclusion("pipeline.open_applications", open_applications)
register_activity_signal("pipeline.stage_changes", stage_signal)
register_ai_derived_purger("pipeline.screening_summaries", purge_ai_summaries, find_ai_summaries)
