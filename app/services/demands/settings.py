"""Demand settings (FR-17, FR-74): SLA targets, business calendar, REQ prefix. ``settings:manage`` only
(enforced by the router)."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import business_hours
from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.demands import DEFAULT_WORK_WEEK, DemandDocument, SlaTarget
from app.schemas.demands import (
    BusinessCalendarIn,
    BusinessCalendarOut,
    DemandDocumentOut,
    DemandSettingsIn,
    DemandSettingsOut,
    SlaTargetOut,
    SlaTargetsIn,
)
from app.services import directory_service as directory
from app.services.demands.common import READ, demand_settings, get_for, tenant_of


async def list_sla_targets(ctx: RequestContext, db: AsyncSession) -> list[SlaTargetOut]:  # ruff: ignore[unused-function-argument]
    rows = (
        await db.execute(
            select(SlaTarget)
            .where(SlaTarget.subject_type == "demand")
            .order_by(SlaTarget.milestone, SlaTarget.priority)
        )
    ).scalars()
    return [SlaTargetOut.model_validate(r) for r in rows]


async def replace_sla_targets(
    ctx: RequestContext, db: AsyncSession, data: SlaTargetsIn
) -> list[SlaTargetOut]:
    keys = [(t.milestone, t.priority) for t in data.targets]
    if len(keys) != len(set(keys)):
        raise ValidationFailed(
            "Each milestone may have one target per priority (and one 'any priority' target)",
            code="duplicate_sla_target",
        )
    existing = {
        (r.milestone, r.priority): r
        for r in (await db.execute(select(SlaTarget).where(SlaTarget.subject_type == "demand"))).scalars()
    }
    for key, stale in existing.items():
        if key not in set(keys):
            await db.delete(stale)
    await db.flush()
    for target in data.targets:
        found = existing.get((target.milestone, target.priority))
        if found is None:
            row = SlaTarget(
                id=uuid.uuid4(), tenant_id=ctx.tenant_id, milestone=target.milestone, priority=target.priority
            )
            db.add(row)
        else:
            row = found
        row.target_hours = target.target_hours
        row.clock = target.clock
        row.at_risk_percent = target.at_risk_percent
    await db.flush()
    return await list_sla_targets(ctx, db)


async def get_calendar(ctx: RequestContext, db: AsyncSession) -> BusinessCalendarOut:
    row = await demand_settings(ctx, db, create=False)
    return BusinessCalendarOut(
        # An unsaved settings row has no column defaults applied yet.
        work_week=row.work_week or DEFAULT_WORK_WEEK,
        holidays=sorted(row.holidays or []),
        timezone=await directory.tenant_timezone(db, tenant_of(db)),
    )


async def put_calendar(
    ctx: RequestContext, db: AsyncSession, data: BusinessCalendarIn
) -> BusinessCalendarOut:
    week: dict[str, Any] = {str(day): window.model_dump() for day, window in data.work_week.items()}
    try:
        business_hours.validate_work_week(week)
    except ValueError as exc:
        raise ValidationFailed(
            str(exc), details={"fields": [{"field": "work_week", "message": str(exc)}]}
        ) from exc
    row = await demand_settings(ctx, db)
    row.work_week = week
    row.holidays = sorted(set(data.holidays))
    await db.flush()
    return await get_calendar(ctx, db)


async def get_demand_settings(ctx: RequestContext, db: AsyncSession) -> DemandSettingsOut:
    return DemandSettingsOut(req_prefix=(await demand_settings(ctx, db, create=False)).req_prefix)


async def put_demand_settings(
    ctx: RequestContext, db: AsyncSession, data: DemandSettingsIn
) -> DemandSettingsOut:
    row = await demand_settings(ctx, db)
    row.req_prefix = data.req_prefix
    await db.flush()
    return DemandSettingsOut(req_prefix=row.req_prefix)


async def list_documents(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID
) -> list[DemandDocumentOut]:
    demand = await get_for(ctx, db, demand_id, READ, for_update=False)
    rows = (
        await db.execute(
            select(DemandDocument)
            .where(DemandDocument.demand_id == demand.id)
            .order_by(DemandDocument.created_at)
        )
    ).scalars()
    return [DemandDocumentOut.model_validate(r) for r in rows]
