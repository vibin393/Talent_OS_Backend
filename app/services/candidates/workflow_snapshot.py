"""Read-only candidate facts for Epic 10 workflow conditions (additive to the candidates service).

Ids, enums, tag names and the city only; never compensation, contact details or free text.
"""

import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.candidates import Candidate, CandidateTag, Tag


async def candidate_tag_names(db: AsyncSession, candidate_id: uuid.UUID) -> list[str]:
    rows = await db.execute(
        select(Tag.name)
        .join(CandidateTag, CandidateTag.tag_id == Tag.id)
        .where(CandidateTag.candidate_id == candidate_id)
        .order_by(Tag.name)
    )
    return [str(n).lower() for n in rows.scalars()]


async def workflow_snapshot(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID  # ruff: ignore[unused-function-argument]
) -> dict[str, Any] | None:
    cand = await db.get(Candidate, candidate_id)
    if cand is None:
        return None
    location = cand.location or {}
    months = cand.total_experience_months
    years = (Decimal(months) / 12).quantize(Decimal("0.1"), ROUND_HALF_UP) if months is not None else None
    return {
        "id": str(cand.id),
        "candidate_id": str(cand.id),
        "source": cand.source,
        "tags": await candidate_tag_names(db, cand.id),
        "owner_id": str(cand.owner_id),
        "location.city": location.get("city") if isinstance(location, dict) else None,
        "experience_years": str(years) if years is not None else None,
    }
