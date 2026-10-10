"""Talent pool settings (spec A1): one ``talent_pool_settings`` row per tenant, created lazily."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.talent_pools import TalentPoolSettings
from app.schemas.talent_pools import PoolSettingsIn, PoolSettingsOut

DEFAULTS = {"nightly_refresh_local_time": "02:00", "digest_local_time": "08:00", "max_dynamic_pools": 2000}


async def get_row(db: AsyncSession, tenant_id: uuid.UUID) -> TalentPoolSettings:
    row = (
        await db.execute(select(TalentPoolSettings).where(TalentPoolSettings.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        row = TalentPoolSettings(tenant_id=tenant_id, **DEFAULTS)
        db.add(row)
        await db.flush()
    return row


def out(row: TalentPoolSettings) -> PoolSettingsOut:
    return PoolSettingsOut(
        nightly_refresh_local_time=row.nightly_refresh_local_time,
        digest_local_time=row.digest_local_time,
        max_dynamic_pools=row.max_dynamic_pools,
    )


async def read(ctx: RequestContext, db: AsyncSession) -> PoolSettingsOut:
    assert ctx.tenant_id is not None
    return out(await get_row(db, ctx.tenant_id))


async def update(ctx: RequestContext, db: AsyncSession, body: PoolSettingsIn) -> PoolSettingsOut:
    assert ctx.tenant_id is not None
    row = await get_row(db, ctx.tenant_id)
    for key in ("nightly_refresh_local_time", "digest_local_time", "max_dynamic_pools"):
        value = getattr(body, key)
        if key in body.model_fields_set and value is not None:
            setattr(row, key, value)
    await db.flush()
    return out(row)
