# ruff: noqa: E501
"""Per-tenant communication channel settings (Epic 33, A1): one ``comms_channel_settings`` row, created lazily.

``GET/PATCH /api/v1/comms/channel-settings`` (``messaging:configure``). Quiet hours apply to SMS only.
"""

import uuid
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.comms import DEFAULT_QUIET_HOURS, CommsChannelSettings
from app.schemas.messaging import ChannelSettingsOut, ChannelSettingsUpdate, QuietHours


def _tenant(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


async def get_row(db: AsyncSession) -> CommsChannelSettings:
    row = (await db.execute(select(CommsChannelSettings))).scalar_one_or_none()
    if row is not None:
        return row
    await db.execute(
        pg_insert(CommsChannelSettings)
        .values(id=uuid.uuid4(), tenant_id=_tenant(db), quiet_hours=dict(DEFAULT_QUIET_HOURS))
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    return (await db.execute(select(CommsChannelSettings))).scalar_one()


def _out(row: CommsChannelSettings) -> ChannelSettingsOut:
    return ChannelSettingsOut(
        sms_enabled=row.sms_enabled,
        whatsapp_enabled=row.whatsapp_enabled,
        default_sms_sender_id=row.default_sms_sender_id,
        dlt_entity_id=row.dlt_entity_id,
        quiet_hours=QuietHours.model_validate({**DEFAULT_QUIET_HOURS, **(row.quiet_hours or {})}),
        inbound_sync_enabled=row.inbound_sync_enabled,
        thread_sync_days=row.thread_sync_days,
    )


async def get_settings(ctx: RequestContext, db: AsyncSession) -> ChannelSettingsOut:  # noqa: ARG001
    return _out(await get_row(db))


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: ChannelSettingsUpdate
) -> ChannelSettingsOut:
    row = await get_row(db)
    changes = data.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if key == "quiet_hours":
            row.quiet_hours = dict(value) if value is not None else dict(DEFAULT_QUIET_HOURS)
        elif value is not None or key in ("default_sms_sender_id", "dlt_entity_id"):
            setattr(row, key, value)
    await db.flush()
    return _out(row)


def quiet_until(quiet_hours: dict[str, str] | None, now: datetime) -> datetime | None:
    """When ``now`` (aware) falls inside the quiet window, the instant it ends; else None.

    The window may wrap midnight (``21:00``-``09:00``). An equal start / end means no quiet hours.
    """
    cfg = {**DEFAULT_QUIET_HOURS, **(quiet_hours or {})}
    try:
        tz = ZoneInfo(cfg["tz"])
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("Asia/Kolkata")
    start = time.fromisoformat(cfg["start"])
    end = time.fromisoformat(cfg["end"])
    if start == end:
        return None
    local = now.astimezone(tz)
    t = local.time()
    inside = (start <= t < end) if start < end else (t >= start or t < end)
    if not inside:
        return None
    end_today = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if end_today <= local:
        from datetime import timedelta

        end_today += timedelta(days=1)
    return end_today
