"""Bench erasure handler ``placements.bench`` (58), Epic 24 A8 (AD-34).

Deletes the erased candidate's ``bench_entries``, ``bench_match_reviews`` and ``bench_match_notices``; bench
reporting aggregates are computed live, so nothing else needs rewriting.
"""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.placements import BenchEntry, BenchMatchNotice, BenchMatchReview
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class BenchHandler:
    name = "placements.bench"
    order = 58
    tables = ("bench_entries", "bench_match_reviews", "bench_match_notices")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        counts: dict[str, int] = {}
        for model, key in (
            (BenchMatchReview, "reviews"),
            (BenchMatchNotice, "notices"),
            (BenchEntry, "entries"),
        ):
            result = await db.execute(delete(model).where(model.candidate_id.in_(ids)))
            counts[key] = int(result.rowcount or 0)
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = list(
            (
                await db.execute(select(BenchEntry).where(BenchEntry.candidate_id == ctx.candidate_id))
            ).scalars()
        )
        if not rows:
            return None
        return {
            "bench_entries": [
                {
                    "status": e.status,
                    "origin": e.origin,
                    "available_from": e.available_from,
                    "work_authorization": e.work_authorization,
                    "preferred_locations": e.preferred_locations,
                    "notes": e.notes,
                    "closed_at": e.closed_at,
                }
                for e in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        left = (
            await db.execute(select(BenchEntry.id).where(BenchEntry.candidate_id.in_(ids)).limit(1))
        ).first()
        return ["bench entry remains"] if left else []


register_erasure_handler(BenchHandler())
