# ruff: file-ignore[line-too-long]
"""Facade for the Cross-tenant Job Network (Epic 75). Routers and other epics call this module, never the internal
``app.services.job_network.*`` submodules directly. Reporting (Epic 79) reads tenant stats through ``tenant_stats``
so it never imports ``models.job_network`` (the ``p5-network-platform`` contract)."""

import uuid
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.job_network import (
    ListingEligibilityOut,
    ListingStatusOut,
    MyListingsPage,
    MyListingsQuery,
    NetworkAnalyticsOut,
    NetworkJoinIn,
    NetworkSettingsOut,
    NetworkSettingsPatch,
)
from app.services.job_network import membership, stats


# ---------------------------------------------------------------- tenant admin
async def get_settings(ctx: RequestContext, db: AsyncSession) -> NetworkSettingsOut:
    return await membership.get_settings_view(ctx, db)


async def join(ctx: RequestContext, db: AsyncSession, data: NetworkJoinIn) -> NetworkSettingsOut:
    return await membership.join(ctx, db, data)


async def update_settings(ctx: RequestContext, db: AsyncSession, data: NetworkSettingsPatch) -> NetworkSettingsOut:
    return await membership.update_settings(ctx, db, data)


async def leave(ctx: RequestContext, db: AsyncSession, *, reason: str | None = None) -> NetworkSettingsOut:
    return await membership.leave(ctx, db, reason=reason)


async def eligibility(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> ListingEligibilityOut:
    return await membership.eligibility(ctx, db, demand_id)


async def set_choice(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, include: bool) -> ListingStatusOut:
    return await membership.set_choice(ctx, db, demand_id, include)


async def my_listings(ctx: RequestContext, db: AsyncSession, q: MyListingsQuery) -> MyListingsPage:
    return await membership.my_listings(ctx, db, q)


# ---------------------------------------------------------------- analytics
async def analytics(
    ctx: RequestContext, db: AsyncSession, *, date_from: date | None = None, date_to: date | None = None, demand_id: uuid.UUID | None = None
) -> NetworkAnalyticsOut:
    return await stats.analytics(ctx, db, date_from=date_from, date_to=date_to, demand_id=demand_id)


async def tenant_stats(ctx: RequestContext, db: AsyncSession, q: Any) -> NetworkAnalyticsOut:  # q: duck-typed stats query (Epic 79 reporting tiles)
    date_from = getattr(q, "date_from", None) or getattr(q, "from_", None)
    date_to = getattr(q, "date_to", None) or getattr(q, "to", None)
    return await stats.analytics(ctx, db, date_from=date_from, date_to=date_to, demand_id=None)


__all__ = [
    "analytics",
    "eligibility",
    "get_settings",
    "join",
    "leave",
    "my_listings",
    "set_choice",
    "tenant_stats",
    "update_settings",
]
