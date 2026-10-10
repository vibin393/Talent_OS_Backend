"""Facade of the ``demand_insights`` domain (Epic 55, A5). Routers, tasks and agent tools import from here."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.demand_insights import AcceptSuggestionIn, DemandInsightsOut, SuggestSkillsIn
from app.services.demand_insights import similarity, sla_risk
from app.services.demand_insights.drafts import apply_update, create_draft, update_draft
from app.services.demand_insights.flags import expire as _expire
from app.services.demand_insights.flags import list_flags, raise_duplicate, resolve
from app.services.demand_insights.jd import bias_matches, has_jd, preview, save_ai_version
from app.services.demand_insights.overview import insights, summary

resolve_flag = resolve


async def suggest_skills(
    ctx: RequestContext, db: AsyncSession, data: SuggestSkillsIn, *, run_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    from app.services.demand_insights import suggestions
    from app.services.demands.common import UPDATE, get_for

    demand = await get_for(ctx, db, data.demand_id, UPDATE, for_update=False)
    return await suggestions.add_skill_suggestions(ctx, db, demand, data.items, run_id=run_id)


async def accept_suggestion(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    data: AcceptSuggestionIn,
) -> DemandInsightsOut:
    from app.services.demand_insights import suggestions

    await suggestions.accept(ctx, db, demand_id, suggestion_id, data)
    return await insights(ctx, db, demand_id)


async def dismiss_suggestion(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, suggestion_id: uuid.UUID
) -> DemandInsightsOut:
    from app.services.demand_insights import suggestions

    await suggestions.dismiss(ctx, db, demand_id, suggestion_id)
    return await insights(ctx, db, demand_id)


async def expire_flags(ctx: RequestContext, db: AsyncSession) -> int:
    return await _expire(ctx, db)


__all__ = [
    "accept_suggestion",
    "apply_update",
    "bias_matches",
    "create_draft",
    "dismiss_suggestion",
    "expire_flags",
    "has_jd",
    "insights",
    "list_flags",
    "preview",
    "raise_duplicate",
    "resolve_flag",
    "save_ai_version",
    "similarity",
    "sla_risk",
    "suggest_skills",
    "summary",
    "update_draft",
]
