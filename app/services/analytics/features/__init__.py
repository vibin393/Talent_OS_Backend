"""Feature specs, the protected-feature denylist and the per-kind extractors (Epic 48 A3).

Extractor modules never import ``app.models.fairness`` / ``app.services.fairness`` (AD-47 import ban, tested).
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.analytics.features.common import ScoringRow, TrainingRow


async def training_rows(db: AsyncSession, kind: str, now: datetime, history_months: int) -> list[TrainingRow]:
    from app.services.analytics.features import attrition, offer_drop, time_to_fill

    module = {"time_to_fill": time_to_fill, "offer_drop": offer_drop, "attrition": attrition}[kind]
    return await module.training_rows(db, now, history_months)  # type: ignore[no-any-return]


async def scoring_rows(
    db: AsyncSession,
    kind: str,
    now: datetime,
    subject_ids: Sequence[uuid.UUID] | None = None,
    subject_kind: str | None = None,
) -> list[ScoringRow]:
    from app.services.analytics.features import attrition, offer_drop, time_to_fill

    if kind == "offer_drop":
        return await offer_drop.scoring_rows(db, now, subject_ids, subject_kind)
    module = {"time_to_fill": time_to_fill, "attrition": attrition}[kind]
    return await module.scoring_rows(db, now, subject_ids)  # type: ignore[no-any-return]


__all__ = ["ScoringRow", "TrainingRow", "scoring_rows", "training_rows"]
