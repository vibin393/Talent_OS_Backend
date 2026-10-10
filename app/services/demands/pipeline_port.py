"""Demand facts the pipeline domain (Epic 6) needs, exposed by the demands domain (AD-1).

The pipeline service never queries demand tables itself for these: accepting applications, the skill
list for screening questions / AI prompts, the JD text and the progress counters go through here.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFound
from app.models.demands import Demand, DemandSkill, JdVersion

ACCEPTING_STATUSES: frozenset[str] = frozenset({"open", "on_hold"})


def assert_demand_accepts_applications(demand: Demand) -> None:
    """409 ``demand_not_accepting`` unless the demand is Open or On Hold (and not archived)."""
    if demand.status not in ACCEPTING_STATUSES or demand.archived_at is not None:
        raise ConflictError(
            "Candidates can only be added to Open or On Hold demands",
            code="demand_not_accepting",
            details={"status": demand.status},
        )


async def get_row(db: AsyncSession, demand_id: uuid.UUID) -> Demand:
    row = await db.get(Demand, demand_id)
    if row is None:
        raise NotFound("Demand not found")
    return row


async def titles(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not ids:
        return {}
    rows = await db.execute(
        select(Demand.id, Demand.title, Demand.req_id, Demand.client_id).where(Demand.id.in_(ids))
    )
    return {i: {"title": t, "req_id": r, "client_id": c} for i, t, r, c in rows.all()}


async def skills(db: AsyncSession, demand_id: uuid.UUID) -> list[dict[str, Any]]:
    """``[{label, kind mandatory|preferred, min_years}]`` in display order."""
    rows = await db.execute(
        select(DemandSkill.skill_label, DemandSkill.kind, DemandSkill.min_years)
        .where(DemandSkill.demand_id == demand_id)
        .order_by(DemandSkill.position, DemandSkill.skill_label)
    )
    return [
        {"label": label, "kind": kind, "min_years": float(years) if years is not None else None}
        for label, kind, years in rows.all()
    ]


async def jd_text(db: AsyncSession, demand: Demand) -> str | None:
    """Return the published JD version, else the latest ``full`` JD version, else None."""
    if demand.published_jd_version_id is not None:
        row = await db.get(JdVersion, demand.published_jd_version_id)
        if row is not None:
            return row.content
    row = (
        await db.execute(
            select(JdVersion)
            .where(JdVersion.demand_id == demand.id, JdVersion.kind == "full")
            .order_by(JdVersion.version_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row.content if row is not None else None
