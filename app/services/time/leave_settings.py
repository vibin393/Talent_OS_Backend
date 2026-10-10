"""Tenant leave settings (Epic 26, A1): ``leave_year`` calendar | fiscal (April-March), ``default_work_week``,
``accrual_day`` (1-28) and ``client_team_calendar_default``. One row per tenant, created lazily."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.time import LeaveSettings
from app.schemas.leave import LeaveSettingsOut, LeaveSettingsUpdate
from app.services.time.common import tenant_of
from app.services.time.holidays import leave_settings_row


def _out(row: LeaveSettings) -> LeaveSettingsOut:
    return LeaveSettingsOut(
        leave_year=row.leave_year,  # type: ignore[arg-type]
        default_work_week=sorted(int(d) for d in row.default_work_week or []),
        accrual_day=row.accrual_day,
        client_team_calendar_default=row.client_team_calendar_default,
    )


async def get_settings(ctx: RequestContext, db: AsyncSession) -> LeaveSettingsOut:
    return _out(await leave_settings_row(db, tenant_of(ctx, db)))


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: LeaveSettingsUpdate
) -> LeaveSettingsOut:
    row = await leave_settings_row(db, tenant_of(ctx, db))
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        setattr(row, key, sorted(set(value)) if key == "default_work_week" else value)
    await db.flush()
    return _out(row)
