# ruff: noqa: E501
"""Erasure handler ``timesheet_flags.flags`` (Epic 63, order 67; C-15, FR-326).

Timesheets are financial records and stay; on a candidate subject's erasure (+ merged ids) the Worker's anomaly flags
keep their rule, severity, numeric facts and status for the approval history, but the free text about the person --
the AI / rules explanation and the reviewer's note -- is nulled. A top-level module, so ``privacy`` never imports the
Phase 4 domain.
"""

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.timesheet_flags import TimesheetFlag
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class TimesheetFlagsHandler:
    name = "timesheet_flags.flags"
    order = 67
    tables = ("timesheet_flags",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"timesheet_flags_redacted": 0}
        ids = await subject_ids(db, ctx.candidate_id)
        res = await db.execute(
            update(TimesheetFlag)
            .where(TimesheetFlag.candidate_id.in_(ids))
            .values(explanation=None, review_note=None, group_key=None, explanation_source="rules")
        )
        await db.flush()
        return {"timesheet_flags_redacted": int(getattr(res, "rowcount", 0) or 0)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        rows = (
            await db.execute(
                select(TimesheetFlag).where(TimesheetFlag.candidate_id == ctx.candidate_id).order_by(TimesheetFlag.created_at)
            )
        ).scalars()
        out = [
            {"rule": r.rule, "severity": r.severity, "status": r.status, "explanation": r.explanation, "raised_at": r.created_at}
            for r in rows
        ]
        return {"timesheet_flags": out} if out else None

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        ids = await subject_ids(db, ctx.candidate_id)
        left = (
            await db.execute(
                select(TimesheetFlag.id)
                .where(TimesheetFlag.candidate_id.in_(ids), (TimesheetFlag.explanation.is_not(None)) | (TimesheetFlag.review_note.is_not(None)))
                .limit(1)
            )
        ).first()
        return ["timesheet flag explanations remain"] if left else []


register_erasure_handler(TimesheetFlagsHandler())
