# ruff: file-ignore[line-too-long]
"""Demand page read models (Epic 55, B3): the "AI assist" tab (field sources, suggestions, flags, SLA risk, last
run) and the compact summary the agent reads (``demands.get_for_review``)."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.demand_insights import DemandInsightsOut, InsightsSummaryOut, LastRunOut
from app.services.demand_insights import flags, provenance, sla_risk, suggestions


async def summary(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> InsightsSummaryOut:
    open_flags: list[dict[str, Any]] = []
    for f in await flags.open_for(db, demand_id):
        related = None
        if f.related_demand_id is not None:
            other = await flags._readable(ctx, db, f.related_demand_id)
            related = other.req_id if other is not None else None
        open_flags.append({"kind": f.kind, "related_human_id": related})
    return InsightsSummaryOut(
        open_flags=open_flags, pending_suggestions=await suggestions.pending_count(db, demand_id)
    )


async def _last_run(db: AsyncSession, demand_id: uuid.UUID) -> LastRunOut | None:
    from app.models.agent_runtime import AgentRun

    row = (
        await db.execute(
            select(AgentRun)
            .where(
                AgentRun.agent_key == "demand",
                AgentRun.subject_type == "demand",
                AgentRun.subject_id == demand_id,
            )
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return (
        LastRunOut(run_id=row.id, status=row.status, created_at=row.created_at) if row is not None else None
    )


async def _pending_jd_proposal(db: AsyncSession, demand_id: uuid.UUID) -> uuid.UUID | None:
    from app.models.agent_runtime import AgentProposal

    return (
        await db.execute(
            select(AgentProposal.id)
            .where(
                AgentProposal.tool_key == "demands.update",
                AgentProposal.target_id == demand_id,
                AgentProposal.status == "pending",
            )
            .order_by(AgentProposal.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def insights(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> DemandInsightsOut:
    from app.services import ai
    from app.services.demands.common import READ, get_for

    demand = await get_for(ctx, db, demand_id, READ, for_update=False)
    features = await ai.features_map(db)
    sla = await sla_risk.assess(ctx, db, demand.id) if demand.status == "open" else None
    return DemandInsightsOut(
        demand_id=demand.id,
        field_sources=await provenance.visible(db, demand.id, list(demand.ai_extracted_fields or [])),
        suggestions=await suggestions.for_demand(db, demand.id),
        flags=await flags.for_demand(ctx, db, demand.id),
        sla=sla,
        agent_enabled=bool(features.get("agentic") and features.get("agent_demand")),
        last_run=await _last_run(db, demand.id),
        pending_jd_proposal_id=await _pending_jd_proposal(db, demand.id),
    )


__all__ = ["insights", "summary"]
