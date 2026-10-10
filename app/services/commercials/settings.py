"""Commercials settings (Epic 16 A1): renewal lead days, PO thresholds / expiry days, exception approvers."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.commercials import CommercialsSettings
from app.schemas.commercials import CommercialsSettingsOut, CommercialsSettingsUpdate
from app.services.commercials.common import tenant_of

# The Finance Admin role (Epic 14) when it exists, else the Tenant Admin (``finance_role`` resolver).
DEFAULT_EXCEPTION_STEPS: list[dict[str, Any]] = [
    {
        "name": "Finance approval",
        "approver_kind": "finance_role",
        "approver_ref": {"role_key": "finance_admin"},
    }
]


async def get_row(db: AsyncSession) -> CommercialsSettings:
    row = (await db.execute(select(CommercialsSettings))).scalar_one_or_none()
    if row is not None:
        return row
    await db.execute(
        insert(CommercialsSettings)
        .values(
            tenant_id=tenant_of(db),
            rate_exception_steps=DEFAULT_EXCEPTION_STEPS,
            po_exception_steps=DEFAULT_EXCEPTION_STEPS,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    return (await db.execute(select(CommercialsSettings))).scalar_one()


async def get_settings(ctx: RequestContext, db: AsyncSession) -> CommercialsSettingsOut:  # noqa: ARG001
    return CommercialsSettingsOut.model_validate(await get_row(db))


async def update_settings(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    data: CommercialsSettingsUpdate,
) -> CommercialsSettingsOut:
    row = await get_row(db)
    changes = data.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in changes.items():
        if key in ("contract_renewal_lead_days", "po_alert_thresholds_pct"):
            value = sorted(set(value), reverse=key == "contract_renewal_lead_days")
        setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    return CommercialsSettingsOut.model_validate(row)
