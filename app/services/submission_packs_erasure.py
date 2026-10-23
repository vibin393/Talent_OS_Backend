# ruff: file-ignore[line-too-long]
"""Submission pack erasure handler ``submission_packs.packs`` (order 61), Epic 61 (FR-326, AD-34).

On Candidate erasure the subject's (and merged ids') packs keep their facts (ids, status, lifecycle, dates) for the
submission history, but the write-up, the checks detail and the diff of the person's changes are nulled; open packs are
discarded (``erased``). ``export`` lists the packs. A top-level module so ``privacy`` never imports the Phase 4 domain.
"""

from typing import Any

from sqlalchemy import null, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.submission_packs import SubmissionPack
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class SubmissionPacksHandler:
    name = "submission_packs.packs"
    order = 61
    tables = ("submission_packs",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        discarded = await db.execute(
            update(SubmissionPack)
            .where(SubmissionPack.candidate_id.in_(ids), SubmissionPack.lifecycle == "open")
            .values(lifecycle="discarded", discarded_reason="erased")
        )
        redacted = await db.execute(
            update(SubmissionPack)
            .where(SubmissionPack.candidate_id.in_(ids))
            .values(summary_text=None, checks_detail=[], user_changes=null(), email_subject=None)
        )
        await db.flush()
        return {
            "packs_redacted": int(getattr(redacted, "rowcount", 0) or 0),
            "packs_discarded": int(getattr(discarded, "rowcount", 0) or 0),
        }

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = (
            await db.execute(
                select(
                    SubmissionPack.human_id,
                    SubmissionPack.status,
                    SubmissionPack.lifecycle,
                    SubmissionPack.summary_text,
                    SubmissionPack.created_at,
                    SubmissionPack.used_at,
                )
                .where(SubmissionPack.candidate_id == ctx.candidate_id)
                .order_by(SubmissionPack.created_at)
            )
        ).all()
        if not rows:
            return None
        return {
            "submission_packs": [
                {
                    "pack": r.human_id,
                    "status": r.status,
                    "lifecycle": r.lifecycle,
                    "write_up": r.summary_text,
                    "prepared_at": r.created_at,
                    "used_at": r.used_at,
                }
                for r in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        left = (
            await db.execute(
                select(SubmissionPack.id)
                .where(SubmissionPack.candidate_id.in_(ids), SubmissionPack.summary_text.is_not(None))
                .limit(1)
            )
        ).first()
        still_open = (
            await db.execute(
                select(SubmissionPack.id)
                .where(SubmissionPack.candidate_id.in_(ids), SubmissionPack.lifecycle == "open")
                .limit(1)
            )
        ).first()
        problems = []
        if left is not None:
            problems.append("submission pack write-ups remain")
        if still_open is not None:
            problems.append("open submission packs remain")
        return problems


register_erasure_handler(SubmissionPacksHandler())
