# ruff: file-ignore[line-too-long]
"""Public functions of the ``workforce`` domain (Epic 44) re-exported by ``workforce_service``.

* ``plan_summary(ctx, db, from, to)`` -- Epic 52 dashboards: plans whose period touches ``[from, to]`` (viewer's
  ``workforce_plan:read`` scope), by status, with approved headcount and slot counts.
* ``import_plan_line`` -- the go-live importer (foundation 0.7, FR-263).
* ``get_settings`` / ``update_settings`` -- standard hours / days per month and the headcount cap per line.
"""

from collections import Counter
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.workforce import HeadcountSlot, WorkforcePlan, WorkforcePlanLine
from app.schemas.workforce import PlanSummaryOut, WorkforceSettingsIn, WorkforceSettingsOut
from app.services.workforce.common import READ, scoped, settings_row
from app.services.workforce.imports import import_plan_line

__all__ = ["get_settings", "import_plan_line", "plan_summary", "update_settings"]


def _settings_out(row: object) -> WorkforceSettingsOut:
    return WorkforceSettingsOut(
        standard_hours_per_month=Decimal(row.standard_hours_per_month),  # type: ignore[attr-defined]
        standard_days_per_month=Decimal(row.standard_days_per_month),  # type: ignore[attr-defined]
        max_headcount_per_line=int(row.max_headcount_per_line),  # type: ignore[attr-defined]
    )


async def get_settings(ctx: RequestContext, db: AsyncSession) -> WorkforceSettingsOut:  # ruff: ignore[unused-function-argument]
    return _settings_out(await settings_row(db))


async def update_settings(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    data: WorkforceSettingsIn,
) -> WorkforceSettingsOut:
    row = await settings_row(db)
    for key, value in data.model_dump(exclude_none=True).items():
        setattr(row, key, value)
    await db.flush()
    return _settings_out(row)


async def plan_summary(
    ctx: RequestContext, db: AsyncSession, period_from: date, period_to: date
) -> PlanSummaryOut:
    plans = list(
        (
            await db.execute(
                scoped(ctx, select(WorkforcePlan), READ).where(
                    WorkforcePlan.period_start <= period_to, WorkforcePlan.period_end >= period_from
                )
            )
        ).scalars()
    )
    by_status = Counter(p.status for p in plans)
    approved = [p for p in plans if p.approved_revision is not None]
    headcount = 0
    for p in approved:
        headcount += int(
            (
                await db.execute(
                    select(func.coalesce(func.sum(WorkforcePlanLine.headcount), 0)).where(
                        WorkforcePlanLine.workforce_plan_id == p.id,
                        WorkforcePlanLine.revision == p.approved_revision,
                        WorkforcePlanLine.change_kind != "removed",
                    )
                )
            ).scalar_one()
        )
    slots: Counter[str] = Counter()
    if plans:
        for status, n in (
            await db.execute(
                select(HeadcountSlot.status, func.count())
                .where(HeadcountSlot.workforce_plan_id.in_([p.id for p in plans]))
                .group_by(HeadcountSlot.status)
            )
        ).tuples():
            slots[status] = int(n)
    return PlanSummaryOut(
        period_from=period_from,
        period_to=period_to,
        plans_by_status=dict(by_status),
        approved_headcount=headcount,
        open_slots=slots["open"],
        requisitioned_slots=slots["requisitioned"],
        filled_slots=slots["filled"],
        cancelled_slots=slots["cancelled"],
    )
