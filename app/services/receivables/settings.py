# ruff: file-ignore[line-too-long]
"""Per-tenant receivables settings (lazy row): aging buckets, auto-allocate order, TDS variance tolerance, PTP grace
days, reminder rules."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.receivables import ReceivablesSettings
from app.schemas.receivables import ReceivablesSettingsOut, ReceivablesSettingsUpdate
from app.services.receivables.common import tenant_of


async def get_settings_row(db: AsyncSession) -> ReceivablesSettings:
    tid = tenant_of(db)
    row = (await db.execute(select(ReceivablesSettings).where(ReceivablesSettings.tenant_id == tid))).scalar_one_or_none()
    if row is None:
        await db.execute(pg_insert(ReceivablesSettings.__table__).values(tenant_id=tid).on_conflict_do_nothing())
        row = (await db.execute(select(ReceivablesSettings).where(ReceivablesSettings.tenant_id == tid))).scalar_one()
    return row


def buckets_of(row: ReceivablesSettings) -> list[int]:
    raw = row.aging_buckets or [30, 60, 90]
    return sorted({int(b) for b in raw if int(b) > 0}) or [30, 60, 90]


async def get_settings(ctx: RequestContext, db: AsyncSession) -> ReceivablesSettingsOut:  # ruff: ignore[unused-function-argument]
    return ReceivablesSettingsOut.model_validate(await get_settings_row(db))


async def update_settings(ctx: RequestContext, db: AsyncSession, data: ReceivablesSettingsUpdate) -> ReceivablesSettingsOut:  # ruff: ignore[unused-function-argument]
    row = await get_settings_row(db)
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, key, sorted(set(value)) if key == "aging_buckets" else value)
    await db.flush()
    return ReceivablesSettingsOut.model_validate(row)
