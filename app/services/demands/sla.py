"""SLA clocks (FR-17, story 4.6).

* On Open, the ``assignment``, ``first_submission`` and ``first_interview`` clocks start (when a target is
  configured for the milestone; a priority-specific target overrides the "any priority" row).
* ``client_feedback`` clocks start per submission (``start_sla_clock`` with ``ref_id``); any clock ends with
  ``complete_sla_clock``. Epics 6/7 call these hooks.
* ``evaluate_tenant`` recomputes open clocks of Open demands and emits ``DemandSlaStatusChanged`` only when
  the status differs from ``last_notified_status`` (idempotent).
"""

import uuid
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import business_hours
from app.core.context import RequestContext
from app.core.events import emit
from app.events.demands import DemandSlaStatusChanged
from app.models.demands import Demand, DemandAssignment, DemandSlaClock, SlaTarget
from app.services import directory_service as directory
from app.services.demands.common import acting_as, now

OPEN_MILESTONES: tuple[str, ...] = ("assignment", "first_submission", "first_interview")
SEVERITY: dict[str, int] = {"met": 0, "healthy": 1, "at_risk": 2, "breached": 3}


async def calendar_for(ctx: RequestContext, db: AsyncSession) -> tuple[business_hours.Calendar, str]:  # ruff: ignore[unused-function-argument]
    """Delegates to ``core.business_hours.tenant_calendar`` (Epic 13)."""
    return await business_hours.tenant_calendar(db)


async def _target(db: AsyncSession, milestone: str, priority: str) -> SlaTarget | None:
    rows = list(
        (
            await db.execute(
                select(SlaTarget).where(SlaTarget.subject_type == "demand", SlaTarget.milestone == milestone)
            )
        ).scalars()
    )
    specific = next((t for t in rows if t.priority == priority), None)
    return specific or next((t for t in rows if t.priority is None), None)


def _due(
    started: datetime, target: SlaTarget | DemandSlaClock, cal: business_hours.Calendar, tz: str
) -> datetime:
    hours = float(target.target_hours)
    if target.clock == "business":
        return business_hours.add_business_hours(started, hours, cal, tz)
    return business_hours.add_calendar_hours(started, hours)


async def start_sla_clock(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    milestone: str,
    ref_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> DemandSlaClock | None:
    """Start a clock (idempotent per demand/milestone/ref). None when no target is configured."""
    demand = await db.get(Demand, demand_id)
    if demand is None:
        return None
    existing = (
        await db.execute(
            select(DemandSlaClock).where(
                DemandSlaClock.demand_id == demand_id,
                DemandSlaClock.milestone == milestone,
                DemandSlaClock.ref_id.is_(None) if ref_id is None else DemandSlaClock.ref_id == ref_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    target = await _target(db, milestone, demand.priority)
    if target is None:
        return None
    started = at or now()
    cal, tz = await calendar_for(ctx, db)
    clock = DemandSlaClock(
        id=uuid.uuid4(),
        tenant_id=demand.tenant_id,
        demand_id=demand_id,
        milestone=milestone,
        ref_id=ref_id,
        started_at=started,
        due_at=_due(started, target, cal, tz),
        status="healthy",
        last_notified_status="healthy",
        target_hours=Decimal(target.target_hours),
        clock=target.clock,
        at_risk_percent=target.at_risk_percent,
    )
    try:
        async with db.begin_nested():
            db.add(clock)
            await db.flush()
    except IntegrityError:  # started concurrently
        return None
    return clock


async def start_open_clocks(ctx: RequestContext, db: AsyncSession, demand: Demand) -> None:
    for milestone in OPEN_MILESTONES:
        await start_sla_clock(ctx, db, demand.id, milestone)


async def complete_sla_clock(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    demand_id: uuid.UUID,
    milestone: str,
    ref_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> None:
    """Stop a running clock (no-op when absent or already completed): met, or breached when late."""
    clock = (
        await db.execute(
            select(DemandSlaClock).where(
                DemandSlaClock.demand_id == demand_id,
                DemandSlaClock.milestone == milestone,
                DemandSlaClock.ref_id.is_(None) if ref_id is None else DemandSlaClock.ref_id == ref_id,
                DemandSlaClock.completed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if clock is None:
        return
    done = at or now()
    clock.completed_at = done
    clock.status = "met" if done <= clock.due_at else "breached"
    clock.last_notified_status = clock.status
    await db.flush()


def compute_status(clock: DemandSlaClock, at: datetime, cal: business_hours.Calendar, tz: str) -> str:
    if clock.completed_at is not None:
        return "met" if clock.completed_at <= clock.due_at else "breached"
    target = float(clock.target_hours)
    if clock.clock == "business":
        elapsed = business_hours.elapsed_business_hours(clock.started_at, at, cal, tz)
    else:
        elapsed = business_hours.elapsed_calendar_hours(clock.started_at, at)
    if at >= clock.due_at or elapsed >= target:
        return "breached"
    if elapsed >= target * clock.at_risk_percent / 100:
        return "at_risk"
    return "healthy"


async def clocks_for(
    db: AsyncSession, demand_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[DemandSlaClock]]:
    ids = list(demand_ids)
    out: dict[uuid.UUID, list[DemandSlaClock]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = await db.execute(
        select(DemandSlaClock)
        .where(DemandSlaClock.demand_id.in_(ids))
        .order_by(DemandSlaClock.started_at, DemandSlaClock.milestone)
    )
    for clock in rows.scalars():
        out[clock.demand_id].append(clock)
    return out


def worst(statuses: Iterable[str]) -> str | None:
    items = list(statuses)
    return max(items, key=lambda s: SEVERITY.get(s, 0)) if items else None


async def _recipients(db: AsyncSession, demand: Demand) -> tuple[uuid.UUID, ...]:
    ids: set[uuid.UUID] = set()
    primary = (
        await db.execute(
            select(DemandAssignment.user_id).where(
                DemandAssignment.demand_id == demand.id,
                DemandAssignment.is_primary.is_(True),
                DemandAssignment.removed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if primary is not None:
        ids.add(primary)
    ids |= await directory.bu_manager_ids(db, demand.business_unit_id)
    return tuple(sorted(ids))


async def evaluate_tenant(ctx: RequestContext, db: AsyncSession, at: datetime | None = None) -> int:
    """Recompute open clocks of Open demands. Returns the number of events emitted."""
    at = at or now()
    cal, tz = await calendar_for(ctx, db)
    rows = (
        await db.execute(
            select(DemandSlaClock, Demand)
            .join(Demand, Demand.id == DemandSlaClock.demand_id)
            .where(
                DemandSlaClock.completed_at.is_(None), Demand.status == "open", Demand.archived_at.is_(None)
            )
            .order_by(DemandSlaClock.demand_id, DemandSlaClock.milestone)
            .with_for_update(of=DemandSlaClock, skip_locked=True)
        )
    ).all()
    emitted = 0
    with acting_as(db, "system"):
        for clock, demand in rows:
            status = compute_status(clock, at, cal, tz)
            if status != clock.status:
                clock.status = status
            if status != clock.last_notified_status:
                clock.last_notified_status = status
                emit(
                    db,
                    DemandSlaStatusChanged(
                        tenant_id=demand.tenant_id,
                        demand_id=demand.id,
                        milestone=clock.milestone,
                        status=status,
                        recipient_ids=await _recipients(db, demand),
                    ),
                )
                emitted += 1
        await db.flush()
    if emitted:
        logger.info("SLA evaluation: {} status change(s)", emitted)
    return emitted
