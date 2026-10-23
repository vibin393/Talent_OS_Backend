# ruff: file-ignore[line-too-long]
"""Erasure handler ``chasing.records`` (Epic 62, order 67; FR-326).

For a candidate subject (+ merged ids): delete the compliance chases (item labels, channels, message refs) and the BGV
provider follow-ups of the subject. A top-level module, so privacy never imports the ``chasing`` package.
"""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chasing import BgvFollowUp, ComplianceChase
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class ChasingHandler:
    name = "chasing.records"
    order = 67
    tables = ("compliance_chases", "bgv_follow_ups")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"compliance_chases": 0}
        ids = await subject_ids(db, ctx.candidate_id)
        chases = await db.execute(delete(ComplianceChase).where(ComplianceChase.candidate_id.in_(ids)))
        fus = await db.execute(delete(BgvFollowUp).where(BgvFollowUp.candidate_id.in_(ids)))
        await db.flush()
        return {
            "compliance_chases": int(getattr(chases, "rowcount", 0) or 0),
            "bgv_follow_ups": int(getattr(fus, "rowcount", 0) or 0),
        }

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        rows = (
            await db.execute(select(ComplianceChase).where(ComplianceChase.candidate_id == ctx.candidate_id))
        ).scalars()
        return {
            "compliance_reminders": [
                {"item": c.item_label, "channels": list(c.channels), "sent_at": c.sent_at} for c in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        ids = await subject_ids(db, ctx.candidate_id)
        left = (
            await db.execute(select(ComplianceChase.id).where(ComplianceChase.candidate_id.in_(ids)).limit(1))
        ).first()
        return ["compliance chases remain"] if left else []


register_erasure_handler(ChasingHandler())
