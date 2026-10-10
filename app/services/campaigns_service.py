# ruff: noqa: E501, ARG001
"""Facade of the ``campaigns`` domain (Epic 39, recruitment marketing). Other domains (Epic 52 global search and
the marketing funnel tile) call only these; ``campaigns.governance.check`` is the single suppression decision."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.campaigns import Campaign
from app.schemas.campaigns import FunnelOut
from app.schemas.search import QuickHit
from app.services.campaigns import analytics
from app.services.campaigns.governance import Decision, Defer, Send, Suppress, check

__all__ = ["Decision", "Defer", "Send", "Suppress", "check", "funnel", "quick_search"]


async def quick_search(ctx: RequestContext, db: AsyncSession, q: str, limit: int) -> list[QuickHit]:
    principal = ctx.principal
    term = (q or "").strip()
    if principal is None or not principal.has("campaign:read") or not term:
        return []
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = (
        await db.execute(
            select(Campaign.id, Campaign.name, Campaign.status, Campaign.channel)
            .where(Campaign.name.ilike(f"%{escaped}%", escape="\\"))
            .order_by(Campaign.updated_at.desc())
            .limit(max(1, min(limit, 50)))
        )
    ).all()
    return [
        QuickHit(
            id=cid,
            type="campaign",
            title=name,
            subtitle=f"{channel} campaign, {status}",
            url=f"/marketing/campaigns/{cid}",
        )
        for cid, name, status, channel in rows
    ]


async def funnel(
    ctx: RequestContext, db: AsyncSession, start: datetime | None, end: datetime | None
) -> FunnelOut:
    return await analytics.overview(db, start, end)


async def campaign_funnel(ctx: RequestContext, db: AsyncSession, campaign_id: uuid.UUID) -> FunnelOut:
    return await analytics.campaign_funnel(db, campaign_id)
