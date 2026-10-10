"""Endpoint-level service functions of ``router/talent_pools.py`` (one call per endpoint, AD-1)."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError
from app.core.permissions import forbidden
from app.models.talent_pools import TalentPoolRefresh
from app.schemas.pipeline import ApplicationsCreateOut
from app.schemas.talent_pools import (
    AddToPipelineIn,
    PoolCreate,
    PoolMembersAddOut,
    PoolMembersIn,
    PoolMembersPage,
    PoolOut,
    PoolPinOut,
    PoolSettingsIn,
    PoolSettingsOut,
    PoolUpdate,
    RefreshOut,
    SubscriptionOut,
    TemplateOut,
)
from app.services.talent_pools import common, members, pools, rediscovery, refresh, settings, templates


def refresh_out(r: TalentPoolRefresh) -> RefreshOut:
    return RefreshOut(
        id=r.id,
        pool_id=r.pool_id,
        trigger=r.trigger,
        status=r.status,
        added=r.added,
        removed=r.removed,
        evaluated=r.evaluated,
        duration_ms=r.duration_ms,
        error_code=r.error_code,
        started_at=r.started_at,
        finished_at=r.finished_at,
    )


async def list_pools(ctx: RequestContext, db: AsyncSession, **kw: Any) -> list[PoolOut]:
    return await pools.list_pools(ctx, db, **kw)


async def create_pool(ctx: RequestContext, db: AsyncSession, body: PoolCreate) -> PoolOut:
    return await pools.out(ctx, db, await pools.create_pool(ctx, db, body))


async def get_pool(ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID) -> PoolOut:
    return await pools.out(ctx, db, await common.get_pool(ctx, db, pool_id))


async def update_pool(ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID, body: PoolUpdate) -> PoolOut:
    return await pools.out(ctx, db, await pools.update_pool(ctx, db, pool_id, body))


async def archive_pool(ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID) -> PoolOut:
    return await pools.out(ctx, db, await pools.archive_pool(ctx, db, pool_id))


async def list_members(
    ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID, *, page: int, page_size: int
) -> PoolMembersPage:
    pool = await common.get_pool(ctx, db, pool_id)
    return await members.list_members(ctx, db, pool, page=page, page_size=page_size)


async def _member_writer(ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID) -> Any:
    principal = common.principal_of(ctx)
    pool = await common.get_pool(ctx, db, pool_id, for_update=True)
    members.require_static(pool)
    if not await common.can_write_members(db, principal, pool):
        raise forbidden(common.WRITE)
    return principal, pool


async def add_members(
    ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID, body: PoolMembersIn
) -> PoolMembersAddOut:
    principal, pool = await _member_writer(ctx, db, pool_id)
    return await members.add_members(ctx, db, pool, list(body.candidate_ids), actor_id=principal.id)


async def remove_member(
    ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID, candidate_id: uuid.UUID
) -> None:
    _, pool = await _member_writer(ctx, db, pool_id)
    await members.remove_member(ctx, db, pool, candidate_id)


async def refresh_pool(ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID) -> RefreshOut:
    """Owner or ``pool:manage``; at most one manual refresh per 5 minutes (429); a running one is returned."""
    principal = common.principal_of(ctx)
    pool = await common.get_pool(ctx, db, pool_id, for_update=True)
    if not (pool.owner_id == principal.id or principal.has(common.MANAGE)):
        raise forbidden(common.MANAGE)
    if pool.kind != "dynamic":
        raise ConflictError("Only dynamic pools are refreshed", code="pool_is_static")
    return refresh_out(await refresh.request_refresh(db, pool, "manual", cooldown=True))


async def list_refreshes(ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID) -> list[RefreshOut]:
    pool = await common.get_pool(ctx, db, pool_id)
    return [refresh_out(r) for r in await refresh.list_refreshes(db, pool.id)]


async def set_subscription(
    ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID, subscribed: bool
) -> SubscriptionOut:
    return await pools.set_subscription(ctx, db, pool_id, subscribed)


async def list_templates(ctx: RequestContext, db: AsyncSession) -> list[TemplateOut]:
    return await templates.list_templates(ctx, db)


async def enable_template(ctx: RequestContext, db: AsyncSession, key: str, batch: str | None) -> PoolOut:
    return await pools.out(ctx, db, await templates.enable(ctx, db, key, batch))


async def read_settings(ctx: RequestContext, db: AsyncSession) -> PoolSettingsOut:
    return await settings.read(ctx, db)


async def update_settings(ctx: RequestContext, db: AsyncSession, body: PoolSettingsIn) -> PoolSettingsOut:
    return await settings.update(ctx, db, body)


async def list_pins(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> list[PoolPinOut]:
    return await rediscovery.list_pins(ctx, db, demand_id)


async def pin(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, pool_id: uuid.UUID) -> PoolPinOut:
    return await rediscovery.pin(ctx, db, demand_id, pool_id)


async def unpin(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, pool_id: uuid.UUID) -> None:
    await rediscovery.unpin(ctx, db, demand_id, pool_id)


async def add_to_pipeline(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, pool_id: uuid.UUID, body: AddToPipelineIn
) -> ApplicationsCreateOut:
    return await rediscovery.add_to_pipeline(ctx, db, demand_id, pool_id, body)
