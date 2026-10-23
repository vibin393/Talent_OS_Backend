# ruff: file-ignore[line-too-long]
"""Predictive analytics erasure handler ``analytics.predictions`` (48), Epic 48 A8 (FR-258, AD-45).

On erasure of a Candidate:

* ``ml_predictions`` where ``candidate_id`` is one of the subject's ids (offer / placement subjects) are deleted;
* every non-expired training snapshot holding the subject's ``candidate_id`` is rewritten without those rows and
  stamped ``rewritten_for_erasure_at``;
* **model coefficients are aggregate and retained** (``ml_models`` is not touched).

``export`` returns the subject's current predictions (kind, band, computed_at; explanations hold no personal data
and are not included). ``verify`` checks that no prediction or snapshot reference remains.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics import MlPrediction, MlTrainingSet
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


async def _sets(db: AsyncSession, ids: list[Any]) -> list[MlTrainingSet]:
    return list(
        (
            await db.execute(
                select(MlTrainingSet).where(
                    MlTrainingSet.candidate_ids.overlap(ids), MlTrainingSet.expires_at > datetime.now(UTC)
                )
            )
        ).scalars()
    )


class AnalyticsHandler:
    name = "analytics.predictions"
    order = 48
    tables: tuple[str, ...] = ("ml_predictions", "ml_training_sets")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        from app.services.analytics import snapshots

        if ctx.subject_kind != "candidate":
            return {}
        ids = await subject_ids(db, ctx.candidate_id)
        res = await db.execute(delete(MlPrediction).where(MlPrediction.candidate_id.in_(ids)))
        counts = {"predictions": int(getattr(res, "rowcount", 0) or 0), "snapshots": 0, "snapshot_rows": 0}
        for ts in await _sets(db, ids):
            counts["snapshot_rows"] += await snapshots.rewrite_without(db, ts, ids)
            counts["snapshots"] += 1
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        rows = list(
            (
                await db.execute(select(MlPrediction).where(MlPrediction.candidate_id == ctx.candidate_id))
            ).scalars()
        )
        if not rows:
            return None
        return {
            "predictions": [
                {"kind": p.kind, "subject": p.subject_kind, "band": p.band, "computed_at": p.computed_at}
                for p in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        ids = await subject_ids(db, ctx.candidate_id)
        findings: list[str] = []
        if (
            await db.execute(select(MlPrediction.id).where(MlPrediction.candidate_id.in_(ids)).limit(1))
        ).first():
            findings.append("predictions remain")
        if await _sets(db, ids):
            findings.append("training snapshots still reference the subject")
        return findings


register_erasure_handler(AnalyticsHandler())
