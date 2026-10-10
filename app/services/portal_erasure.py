"""Portal-domain erasure handler ``portal.account`` (80) (Epic 9 for Epic 12, FR-68).

* ``application_answers`` of the candidate's applications -> answers cleared (the question snapshot stays).
* ``data_export_jobs`` -> export zips deleted from storage, jobs ``expired``.
* Candidate portal sessions revoked; unsent confirmation addresses on apply sessions cleared.
Retention: a portal sign-in (session) counts as activity.
"""

import uuid
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.models.identity import Session as AuthSession
from app.models.pipeline import Application
from app.models.portal import ApplicationAnswer, ApplySession, DataExportJob
from app.services.candidates_erasure import subject_ids
from app.services.privacy.common import now
from app.services.privacy.registry import ErasureContext, register_activity_signal, register_erasure_handler


async def _application_ids(db: AsyncSession, candidate_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    return list(
        (
            await db.execute(select(Application.id).where(Application.candidate_id.in_(candidate_ids)))
        ).scalars()
    )


class PortalAccountHandler:
    name = "portal.account"
    order = 80
    tables = ("application_answers", "data_export_jobs")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        app_ids = await _application_ids(db, ids)
        answers = list(
            (
                await db.execute(
                    select(ApplicationAnswer).where(
                        ApplicationAnswer.application_id.in_(app_ids or [uuid.uuid4()])
                    )
                )
            ).scalars()
        )
        for row in answers:
            row.answers = {}
        jobs = list(
            (await db.execute(select(DataExportJob).where(DataExportJob.candidate_id.in_(ids)))).scalars()
        )
        deleted = 0
        for job in jobs:
            if job.storage_key:
                try:
                    await storage.get_storage().delete(job.storage_key)
                    deleted += 1
                except FileNotFoundError:
                    pass
                except Exception:
                    logger.exception("Could not delete export {}", job.id)
                    raise
                job.storage_key = None
            if job.status in ("queued", "running", "ready"):
                job.status = "expired"
        await db.execute(
            update(ApplySession)
            .where(ApplySession.application_id.in_(app_ids or [uuid.uuid4()]))
            .values(confirmation_email=None, resume_text=None, parsed_data=None, prefill=None)
        )
        sessions = await db.execute(
            update(AuthSession)
            .where(
                AuthSession.principal_type == "candidate",
                AuthSession.principal_id.in_(ids),
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=now())
        )
        await db.flush()
        return {
            "application_answers": len(answers),
            "exports_deleted": deleted,
            "sessions_revoked": int(sessions.rowcount or 0),  # type: ignore[attr-defined]
        }

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        app_ids = await _application_ids(db, [ctx.candidate_id])
        if not app_ids:
            return None
        rows = (
            await db.execute(select(ApplicationAnswer).where(ApplicationAnswer.application_id.in_(app_ids)))
        ).scalars()
        return {
            "application_answers": [
                {
                    "application_id": str(r.application_id),
                    "submitted_at": r.submitted_at,
                    "answers": r.answers,
                }
                for r in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        app_ids = await _application_ids(db, ids)
        findings: list[str] = []
        if app_ids:
            left = await db.execute(
                select(ApplicationAnswer.id).where(
                    ApplicationAnswer.application_id.in_(app_ids), ApplicationAnswer.answers != {}
                )
            )
            if left.first():
                findings.append("application answers remain")
        exports = await db.execute(
            select(DataExportJob.id).where(
                DataExportJob.candidate_id.in_(ids), DataExportJob.storage_key.is_not(None)
            )
        )
        findings += [f"data export {jid} not deleted" for jid in exports.scalars()]
        return findings


async def _last_sign_in(db: AsyncSession, candidate_ids: list[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    if not candidate_ids:
        return {}
    rows = await db.execute(
        select(AuthSession.principal_id, func.max(AuthSession.last_seen_at))
        .where(AuthSession.principal_type == "candidate", AuthSession.principal_id.in_(candidate_ids))
        .group_by(AuthSession.principal_id)
    )
    return {cid: at for cid, at in rows.all() if at is not None}


register_erasure_handler(PortalAccountHandler())
register_activity_signal("portal.sign_in", _last_sign_in)
