"""Personal dashboard pins (Epic 47 story 47.3; coordinator decision 12: ``report_pins`` + a "Pinned reports"
section on the dashboards page).

``POST / DELETE /reports/custom/{id}/pin`` and ``PUT /me/report-pins/order`` need visibility of the report;
``GET /me/report-pins`` lists the caller's pins that are still visible (each renders through a normal
viewer-scoped run -- ``runs.run_report(..., cached=True)``).
"""

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.report_builder import ReportDefinition, ReportPin
from app.schemas.report_builder import PinOut, VizSpec
from app.services.reporting.builder.common import principal_of
from app.services.reporting.builder.sharing import visible_condition, visible_report

__all__ = ["list_pins", "order_pins", "pin", "unpin"]

MAX_PINS = 24


async def list_pins(ctx: RequestContext, db: AsyncSession) -> list[PinOut]:
    principal = principal_of(ctx)
    rows = (
        await db.execute(
            select(ReportPin, ReportDefinition)
            .join(ReportDefinition, ReportDefinition.id == ReportPin.report_id)
            .where(
                ReportPin.user_id == principal.id,
                ReportDefinition.archived_at.is_(None),
                visible_condition(principal),
            )
            .order_by(ReportPin.position, ReportPin.created_at)
        )
    ).all()
    return [
        PinOut(
            report_id=r.id,
            name=r.name,
            position=p.position,
            viz=VizSpec.model_validate(p.viz_override or r.viz),
            status=r.status,
            definition_version=r.definition_version,
        )
        for p, r in rows
    ]


async def pin(ctx: RequestContext, db: AsyncSession, report_id: uuid.UUID) -> list[PinOut]:
    principal = principal_of(ctx)
    report = await visible_report(db, principal, report_id)
    existing = (
        await db.execute(
            select(ReportPin).where(ReportPin.user_id == principal.id, ReportPin.report_id == report.id)
        )
    ).scalar_one_or_none()
    if existing is None:
        count = (await db.execute(select(func.count()).where(ReportPin.user_id == principal.id))).scalar_one()
        if count >= MAX_PINS:
            raise ValidationFailed(f"At most {MAX_PINS} pinned reports", code="report_pin_limit")
        top = (
            await db.execute(
                select(func.coalesce(func.max(ReportPin.position), -1)).where(
                    ReportPin.user_id == principal.id
                )
            )
        ).scalar_one()
        db.add(
            ReportPin(
                tenant_id=ctx.tenant_id, user_id=principal.id, report_id=report.id, position=int(top) + 1
            )
        )
        await db.flush()
    return await list_pins(ctx, db)


async def unpin(ctx: RequestContext, db: AsyncSession, report_id: uuid.UUID) -> list[PinOut]:
    principal = principal_of(ctx)
    await db.execute(
        delete(ReportPin).where(ReportPin.user_id == principal.id, ReportPin.report_id == report_id)
    )
    await db.flush()
    return await list_pins(ctx, db)


async def order_pins(ctx: RequestContext, db: AsyncSession, report_ids: list[uuid.UUID]) -> list[PinOut]:
    principal = principal_of(ctx)
    pins = {
        p.report_id: p
        for p in (await db.execute(select(ReportPin).where(ReportPin.user_id == principal.id))).scalars()
    }
    position = 0
    for rid in dict.fromkeys(report_ids):
        pin_row = pins.pop(rid, None)
        if pin_row is not None:
            await visible_report(db, principal, rid)
            pin_row.position = position
            position += 1
    for pin_row in sorted(pins.values(), key=lambda p: p.position):
        pin_row.position = position
        position += 1
    await db.flush()
    return await list_pins(ctx, db)
