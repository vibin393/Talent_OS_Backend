# ruff: noqa: E501, D401
"""Skills graph settings (Epic 37 A1): one ``skill_graph_settings`` row per tenant, created lazily.

``match_depth`` / ``min_weight`` are what matching passes to ``expand``; search uses depth 2 and ``min_weight``.
"""

import dataclasses
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationFailed
from app.models.skills_graph import SkillGraphSettings

DEFAULT_MIN_WEIGHT = Decimal("0.30")
DEFAULT_DEPTH = 2


@dataclasses.dataclass(frozen=True, slots=True)
class GraphSettings:
    expansion_default: bool = True
    match_depth: int = DEFAULT_DEPTH
    min_weight: Decimal = DEFAULT_MIN_WEIGHT
    inference_auto_suggest: bool = True

    def fingerprint(self) -> str:
        return f"{int(self.expansion_default)}:{self.match_depth}:{self.min_weight}:{int(self.inference_auto_suggest)}"


def _of(row: SkillGraphSettings | None) -> GraphSettings:
    if row is None:
        return GraphSettings()
    return GraphSettings(
        expansion_default=row.expansion_default,
        match_depth=int(row.match_depth),
        min_weight=Decimal(row.min_weight),
        inference_auto_suggest=row.inference_auto_suggest,
    )


async def current(db: AsyncSession) -> GraphSettings:
    """The tenant's settings (defaults when no row exists yet). RLS scopes the read to the session tenant."""
    row = (await db.execute(select(SkillGraphSettings))).scalar_one_or_none()
    return _of(row)


async def locked_row(db: AsyncSession, tenant_id: uuid.UUID) -> SkillGraphSettings:
    """The settings row ``SELECT ... FOR UPDATE`` (created if missing) -- serialises tenant graph writes."""
    await db.execute(
        insert(SkillGraphSettings)
        .values(tenant_id=tenant_id)
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    row = (
        await db.execute(
            select(SkillGraphSettings)
            .where(SkillGraphSettings.tenant_id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row


def parse_weight(raw: Any, field: str = "weight") -> Decimal:
    """``(0, 1]`` with at most 2 decimals, else 422 ``skill_edge_weight_invalid``."""
    err = ValidationFailed(
        "Weight must be a number greater than 0 and at most 1",
        code="skill_edge_weight_invalid",
        details={"fields": [{"field": field, "message": "Must be greater than 0 and at most 1"}]},
    )
    if isinstance(raw, bool) or raw is None:
        raise err
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise err from None
    if not value.is_finite() or value <= 0 or value > 1 or value != value.quantize(Decimal("0.01")):
        raise err
    return value.quantize(Decimal("0.01"))
