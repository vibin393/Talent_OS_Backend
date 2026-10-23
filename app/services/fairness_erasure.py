# ruff: file-ignore[line-too-long]
"""Fairness erasure handler ``fairness.self_id`` (order 54), Epic 49 A4 (NFR-38, FR-68).

On Candidate erasure: the sealed self-ID row (under ``app.fairness_access = 'erasure'``), the alternative-process
requests and the apply-session self-ID markers are deleted. Past ``fairness_runs`` snapshots and publications are
aggregate-only business records and are kept. ``export`` never returns answer values -- only that answers are on file.
"""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models.fairness  # ruff: ignore[unused-import]  - tables on Base.metadata for the erasure-coverage guard
from app.models.fairness import AedtAlternativeRequest, SelfIdApplyUse
from app.services.candidates_erasure import subject_ids
from app.services.fairness import self_id
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class FairnessHandler:
    name = "fairness.self_id"
    order = 54
    tables = ("demographic_self_ids", "aedt_alternative_requests", "self_id_apply_uses")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = list(await subject_ids(db, ctx.candidate_id))
        counts = {"self_ids": await self_id.delete_for_candidate(db, ids, reason="erasure")}
        res = await db.execute(
            delete(AedtAlternativeRequest).where(AedtAlternativeRequest.candidate_id.in_(ids))
        )
        counts["alternative_requests"] = int(getattr(res, "rowcount", 0) or 0)
        res = await db.execute(delete(SelfIdApplyUse).where(SelfIdApplyUse.candidate_id.in_(ids)))
        counts["apply_uses"] = int(getattr(res, "rowcount", 0) or 0)
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if not await self_id.remaining(db, [ctx.candidate_id]):
            return None
        return {"self_id": "on file; view or change it in your Candidate Portal"}

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = list(await subject_ids(db, ctx.candidate_id))
        findings: list[str] = []
        if await self_id.remaining(db, ids):
            findings.append("self-identification answers remain")
        if (
            await db.execute(
                select(AedtAlternativeRequest.id).where(AedtAlternativeRequest.candidate_id.in_(ids))
            )
        ).first():
            findings.append("alternative-process requests remain")
        return findings


register_erasure_handler(FairnessHandler())
