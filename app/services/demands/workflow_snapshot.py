"""Read-only demand facts for Epic 10 (workflow conditions, notification recipients, task owners).

Additive to the demands service: ``workflow_snapshot`` returns ids and enums only (never compensation or
free text other than the job location); ``primary_recruiter_id`` / ``assignee_user_ids`` resolve the
assignment rows (teams expanded to their active members). All functions take a tenant session (RLS).
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.demands import Demand, DemandAssignment, DemandSkill, DemandSlaClock
from app.services import directory_service as directory

_SLA_RANK = {"met": 0, "healthy": 1, "at_risk": 2, "breached": 3}


async def workflow_snapshot(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID  # ruff: ignore[unused-function-argument]
) -> dict[str, Any] | None:
    demand = await db.get(Demand, demand_id)
    if demand is None:
        return None
    skills = (
        await db.execute(
            select(DemandSkill.skill_label)
            .where(DemandSkill.demand_id == demand_id, DemandSkill.kind == "mandatory")
            .order_by(DemandSkill.position)
        )
    ).scalars()
    clocks = (
        await db.execute(
            select(DemandSlaClock.status).where(
                DemandSlaClock.demand_id == demand_id, DemandSlaClock.completed_at.is_(None)
            )
        )
    ).scalars()
    worst = max(clocks, key=lambda s: _SLA_RANK.get(s, 0), default=None)
    return {
        "id": str(demand.id),
        "status": demand.status,
        "priority": demand.priority,
        "employment_type": demand.employment_type,
        "client_id": str(demand.client_id),
        "business_unit_id": str(demand.business_unit_id) if demand.business_unit_id else None,
        "positions": demand.positions,
        "work_mode": demand.work_mode,
        "location.city": demand.location,
        "required_skills": [s.lower() for s in skills],
        "sla.status": worst,
        "owner_id": str(demand.owner_id),
        "primary_recruiter_id": str(p) if (p := await primary_recruiter_id(db, demand_id)) else None,
        "demand_id": str(demand.id),
    }


async def primary_recruiter_id(db: AsyncSession, demand_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await db.execute(
            select(DemandAssignment.user_id).where(
                DemandAssignment.demand_id == demand_id,
                DemandAssignment.removed_at.is_(None),
                DemandAssignment.is_primary.is_(True),
            )
        )
    ).scalar_one_or_none()


async def assignee_user_ids(db: AsyncSession, demand_id: uuid.UUID) -> set[uuid.UUID]:
    rows = (
        await db.execute(
            select(DemandAssignment.user_id, DemandAssignment.team_id).where(
                DemandAssignment.demand_id == demand_id, DemandAssignment.removed_at.is_(None)
            )
        )
    ).all()
    users = {u for u, _ in rows if u is not None}
    teams = {t for _, t in rows if t is not None}
    return users | await directory.team_member_ids(db, teams)
