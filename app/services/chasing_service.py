# ruff: noqa: E501
"""Facade of the ``chasing`` domain (Epic 62, FR-308 / FR-309). Routers, tools, agents and tasks call these, never the
package internals."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.services.chasing import bgv_follow_ups, chases, plan, templates, ticks
from app.services.chasing.chases import CADENCE
from app.services.chasing.risk_list import risk_list


async def compliance_owner(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    subject: tuple[str, uuid.UUID] | None,
) -> uuid.UUID | None:
    """Accountable Owner of a Compliance Agent run: the Placement's onboarding owner -> default HR owner -> none."""
    if subject is None or subject[0] != "candidate":
        return None
    from app.services.chasing import facts

    bundle_placements = await chases.compute(db, subject[1])
    return bundle_placements.bundle.coordinator_id or await facts.coordinator_for(db, subject[1], [])


async def bgv_owner(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    subject: tuple[str, uuid.UUID] | None,
) -> uuid.UUID | None:
    """Accountable Owner of a BGV Agent run: the BGV case owner."""
    if subject is None or subject[0] != "bgv_case":
        return None
    from app.models.bgv import BgvCase

    case = await db.get(BgvCase, subject[1])
    return await bgv_follow_ups.case_owner(db, case) if case is not None else None


__all__ = [
    "CADENCE",
    "bgv_follow_ups",
    "bgv_owner",
    "chases",
    "compliance_owner",
    "plan",
    "risk_list",
    "templates",
    "ticks",
]
