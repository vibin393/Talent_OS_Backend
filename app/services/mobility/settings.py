# ruff: noqa: E501
"""Mobility settings (Epic 41 A1): ``GET/PATCH /api/v1/mobility/settings`` (``mobility:manage``)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.mobility import MobilitySettings
from app.schemas.mobility import MobilitySettingsOut, MobilitySettingsPatch
from app.services.mobility.common import MANAGE, invalid, require, settings_row


def settings_out(row: MobilitySettings) -> MobilitySettingsOut:
    return MobilitySettingsOut(
        board_enabled=row.board_enabled,
        worker_board_enabled=row.worker_board_enabled,
        screen_before_manager=row.screen_before_manager,
        fallback_approver_role_key=row.fallback_approver_role_key,
        min_tenure_default_months=row.min_tenure_default_months,
        disclosure_text=row.disclosure_text,
    )


async def get_settings(ctx: RequestContext, db: AsyncSession) -> MobilitySettingsOut:
    require(ctx, MANAGE)
    return settings_out(await settings_row(db))


async def update_settings(ctx: RequestContext, db: AsyncSession, data: MobilitySettingsPatch) -> MobilitySettingsOut:
    require(ctx, MANAGE)
    row = await settings_row(db)
    changes = data.model_dump(exclude_unset=True)
    for key in ("board_enabled", "worker_board_enabled", "screen_before_manager", "fallback_approver_role_key", "disclosure_text"):
        if key in changes and changes[key] is None:
            raise invalid("value_required", f"{key} cannot be empty", field=key)
    if "fallback_approver_role_key" in changes:
        from app.services import directory_service

        if await directory_service.role_id_by_key(db, changes["fallback_approver_role_key"]) is None:
            raise invalid("unknown_role", "No role has this key", field="fallback_approver_role_key")
    for key, value in changes.items():
        setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    return settings_out(row)
