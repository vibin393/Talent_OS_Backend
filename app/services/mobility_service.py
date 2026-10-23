# ruff: file-ignore[line-too-long]
"""Facade of the ``mobility`` domain (Epic 41, FR-209..FR-212). Other domains import from here.

Provided (foundation 0.5 / Epic 52): ``internal_fill_rate(ctx, db, from, to)`` for the mobility tile; approval
subject ``internal_application`` and matching section ``internal`` are registered from ``mobility/hooks.py``.
"""

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.mobility import FillRateOut
from app.services.mobility import applications, board, eligibility, employees, matches, settings, transfers
from app.services.mobility.matches import fit_for_employee

__all__ = [
    "applications",
    "board",
    "eligibility",
    "employees",
    "fit_for_employee",
    "internal_fill_rate",
    "matches",
    "settings",
    "transfers",
]


async def internal_fill_rate(ctx: RequestContext, db: AsyncSession, from_date: date, to_date: date) -> FillRateOut:
    """Hires in ``[from_date, to_date]`` made through internal transfers vs all Applications moved to ``hired``."""
    from app.models.mobility import InternalTransfer
    from app.models.pipeline import ApplicationStageHistory, PipelineStage

    del ctx
    start = datetime.combine(from_date, time.min, tzinfo=UTC)
    end = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=UTC)
    internal = int(
        (
            await db.execute(
                select(func.count()).select_from(InternalTransfer).where(InternalTransfer.confirmed_at >= start, InternalTransfer.confirmed_at < end)
            )
        ).scalar_one()
    )
    total = int(
        (
            await db.execute(
                select(func.count(func.distinct(ApplicationStageHistory.application_id)))
                .join(PipelineStage, PipelineStage.id == ApplicationStageHistory.to_stage_id)
                .where(PipelineStage.key == "hired", ApplicationStageHistory.at >= start, ApplicationStageHistory.at < end)
            )
        ).scalar_one()
    )
    return FillRateOut(filled_internal=internal, filled_total=total, rate_pct=round(100.0 * internal / total, 1) if total else None)
