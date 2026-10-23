"""Telephony settings (Epic 76, spec A1/B3): one row per tenant, created lazily with the defaults; audited."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.telephony import TelephonySettings
from app.schemas.telephony import TelephonySettingsIn, TelephonySettingsOut
from app.services.telephony import common


async def row(db: AsyncSession) -> TelephonySettings:
    tenant_id = common.tenant_of(db)
    found = (
        await db.execute(select(TelephonySettings).where(TelephonySettings.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if found is None:
        found = TelephonySettings(tenant_id=tenant_id)
        db.add(found)
        await db.flush()
        await db.refresh(found)
    return found


def out(r: TelephonySettings) -> TelephonySettingsOut:
    return TelephonySettingsOut.model_validate(r)


async def get(ctx: RequestContext, db: AsyncSession) -> TelephonySettingsOut:  # ruff: ignore[unused-function-argument]
    return out(await row(db))


async def update(ctx: RequestContext, db: AsyncSession, data: TelephonySettingsIn) -> TelephonySettingsOut:
    r = await row(db)
    changes = data.model_dump(exclude_unset=True)
    start = changes.get("call_window_start", r.call_window_start)
    end = changes.get("call_window_end", r.call_window_end)
    if end <= start:
        raise common.invalid(
            "invalid_call_window",
            "Calling hours must end after they start",
            fields={"call_window_end": "Must be after the start"},
        )
    for key, value in changes.items():
        setattr(r, key, value)
    r.updated_by = common.user_id_of(ctx)
    await db.flush()
    await db.refresh(r)
    return out(r)
