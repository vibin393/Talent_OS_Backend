# ruff: noqa: E501
"""Fairness settings (Epic 49 A1): one row per tenant, created lazily with the defaults; edits audited (the model is
``AuditedMixin``). Category label edits create a new category set version."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.models.fairness import FairnessSettings
from app.schemas.fairness import (
    CategorySetIn,
    CategorySetOut,
    DimensionOut,
    FairnessSettingsOut,
    FairnessSettingsPatch,
)
from app.services.fairness import categories
from app.services.fairness.common import tenant_of


async def current(db: AsyncSession) -> FairnessSettings:
    row = await db.get(FairnessSettings, tenant_of(db))
    if row is None:
        cset = await categories.active_set(db)
        row = FairnessSettings(
            tenant_id=tenant_of(db),
            self_id_enabled=False,
            self_id_on_apply=True,
            self_id_in_portal=True,
            category_set_id=cset.id,
            k=5,
            ll144_enabled=False,
            notice_lead_business_days=10,
        )
        db.add(row)
        await db.flush()
    return row


async def category_set_out(db: AsyncSession) -> CategorySetOut:
    cset = await categories.active_set(db)
    dims = await categories.dimensions(db, cset.id)
    return CategorySetOut(
        id=cset.id,
        version=cset.version,
        dimensions=[DimensionOut.model_validate(categories.dimension_out(d)) for d in dims],
    )


async def out(db: AsyncSession) -> FairnessSettingsOut:
    s = await current(db)
    return FairnessSettingsOut(
        self_id_enabled=s.self_id_enabled,
        self_id_on_apply=s.self_id_on_apply,
        self_id_in_portal=s.self_id_in_portal,
        k=s.k,
        ll144_enabled=s.ll144_enabled,
        ll144_notice_text=s.ll144_notice_text,
        alternative_process_text=s.alternative_process_text,
        data_retention_text=s.data_retention_text,
        qualifications_text=s.qualifications_text,
        notice_lead_business_days=s.notice_lead_business_days,
        category_set=await category_set_out(db),
    )


async def get(ctx: RequestContext, db: AsyncSession) -> FairnessSettingsOut:  # noqa: ARG001
    return await out(db)


async def patch(ctx: RequestContext, db: AsyncSession, data: FairnessSettingsPatch) -> FairnessSettingsOut:  # noqa: ARG001
    s = await current(db)
    values: dict[str, Any] = data.model_dump(exclude_unset=True)
    for key, value in values.items():
        if value is None and key in {
            "self_id_enabled", "self_id_on_apply", "self_id_in_portal", "k", "ll144_enabled", "notice_lead_business_days",
        }:  # fmt: skip
            continue
        if isinstance(value, str):
            value = value.strip() or None
        setattr(s, key, value)
    await db.flush()
    return await out(db)


async def update_category_set(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    data: CategorySetIn,
) -> FairnessSettingsOut:
    new = await categories.update_labels(db, [d.model_dump() for d in data.dimensions])
    s = await current(db)
    s.category_set_id = new.id
    await db.flush()
    audit.record(
        "fairness.category_set_changed", "fairness_settings", s.tenant_id, session=db, version=new.version
    )
    return await out(db)
