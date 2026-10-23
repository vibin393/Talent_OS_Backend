# ruff: file-ignore[line-too-long]
"""Field provenance (Epic 55, story 55.2): the verbatim source quote of every AI-extracted demand field.

The UI shows a quote only while its field is still in ``demands.ai_extracted_fields`` (P1 semantics: editing or
confirming a field removes it)."""

import uuid
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.demand_insights import DemandFieldSource
from app.schemas.demand_insights import FieldSourceOut

QUOTE_MAX = 500


async def record(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    demand_id: uuid.UUID,
    quotes: Mapping[str, str],
    *,
    intake_message_id: uuid.UUID | None,
    run_id: uuid.UUID | None,
) -> None:
    for field, quote in quotes.items():
        if not quote:
            continue
        stmt = pg_insert(DemandFieldSource).values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            demand_id=demand_id,
            field=field[:60],
            source_quote=quote[:QUOTE_MAX],
            intake_message_id=intake_message_id,
            run_id=run_id,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "demand_id", "field"],
            set_={"source_quote": stmt.excluded.source_quote, "run_id": stmt.excluded.run_id},
        )
        await db.execute(stmt)


async def visible(db: AsyncSession, demand_id: uuid.UUID, ai_fields: list[str]) -> list[FieldSourceOut]:
    rows = (
        await db.execute(
            select(DemandFieldSource)
            .where(DemandFieldSource.demand_id == demand_id)
            .order_by(DemandFieldSource.field)
        )
    ).scalars()
    current = set(ai_fields or [])
    return [
        FieldSourceOut(field=r.field, source_quote=r.source_quote)
        for r in rows
        if r.field in current and r.source_quote
    ]


__all__ = ["record", "visible"]
