"""Time contract (Epic 25): billing / pay inputs of a placement for a period, plus the read functions other
domains use (Epic 13 contract layer AD-36; signatures of ``billing_inputs`` / ``pay_inputs`` are frozen in
``tests/contracts/signatures.json``).

Working days and proration come only from ``time/calendar.py`` (AD-20): ``working_days`` counts the
placement's
work days in ``[max(period_start, actual_start), min(period_end, end_date)]`` minus holidays (the ``holidays``
prefill source, Epic 26); ``month_working_days`` is the month of ``period_start``; ``proration_factor`` =
their
ratio (6 dp). ``hourly_equivalent`` = monthly segment pay rate / (month_working_days x hours_per_day),
unrounded
(``None`` for hourly / daily rates).
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.money import MONEY_CONTEXT
from app.models.time import TimesheetConfig
from app.schemas.time import MonitorOut, TimeBillingInputs, TimePayInputs, TimesheetState, UnapprovedTimesheet
from app.services.time import calendar, monitoring, prefill
from app.services.time.common import dec, get_placement
from app.services.time.configs import config_on


async def _calendar_facts(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, period_start: date, period_end: date
) -> tuple[TimesheetConfig, int, int, Decimal]:
    placement = await get_placement(db, placement_id)
    cfg = await config_on(ctx, db, placement, period_start)
    week = list(cfg.work_days or calendar.DEFAULT_WORK_WEEK)
    first, last = calendar.month_bounds(period_start)
    holidays = await prefill.holiday_dates(db, placement_id, min(first, period_start), max(last, period_end))
    lo = max(d for d in (period_start, placement.actual_start_date) if d is not None)
    hi = min(d for d in (period_end, placement.end_date) if d is not None)
    worked = calendar.working_days(lo, hi, week, holidays) if hi >= lo else 0
    month = calendar.month_working_days(period_start, week, holidays)
    if hi >= lo:
        factor = calendar.proration_factor(lo, min(hi, last), week, holidays)
    else:
        factor = Decimal("0.000000")
    return cfg, worked, month, factor


async def billing_inputs(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, period_start: date, period_end: date
) -> TimeBillingInputs:
    cfg, worked, month, factor = await _calendar_facts(ctx, db, placement_id, period_start, period_end)
    bill = {k: dec(v) for k, v in (cfg.bill_multipliers or {}).items()}
    return TimeBillingInputs(
        config_version=cfg.version,
        holiday_calendar_id=cfg.holiday_calendar_id,
        billable_categories=[c for c in (cfg.enabled_categories or []) if bill.get(c, Decimal(0)) > 0],
        bill_multipliers=bill,
        daily_cap_hours=cfg.daily_cap,
        hours_per_day=cfg.hours_per_day,
        working_days=worked,
        month_working_days=month,
        proration_factor=factor,
    )


async def pay_inputs(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, period_start: date, period_end: date
) -> TimePayInputs:
    from app.services import placements_service

    cfg, worked, month, factor = await _calendar_facts(ctx, db, placement_id, period_start, period_end)
    hourly: Decimal | None = None
    placement = await get_placement(db, placement_id)
    on = max(d for d in (period_start, placement.actual_start_date) if d is not None)
    if placement.end_date is not None and on > placement.end_date:
        on = placement.end_date
    try:
        context = await placements_service.pay_context(ctx, db, placement_id, on)
    except NotFound:
        context = None
    rate = None
    if context is not None:
        rate = context.vendor_rate if context.payee_kind == "vendor" else context.pay_rate
    if (
        context is not None
        and context.rate_unit == "month"
        and rate is not None
        and month
        and cfg.hours_per_day
    ):
        hourly = MONEY_CONTEXT.divide(
            Decimal(rate), MONEY_CONTEXT.multiply(Decimal(month), Decimal(cfg.hours_per_day))
        )
    return TimePayInputs(
        config_version=cfg.version,
        holiday_calendar_id=cfg.holiday_calendar_id,
        pay_multipliers={k: dec(v) for k, v in (cfg.pay_multipliers or {}).items()},
        hours_per_day=cfg.hours_per_day,
        working_days=worked,
        month_working_days=month,
        proration_factor=factor,
        hourly_equivalent=hourly,
    )


async def timesheet_state_for(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, on_date: date
) -> TimesheetState | None:
    return await monitoring.timesheet_state_for(ctx, db, placement_id, on_date)


async def monitor(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    period_start: date | None = None,
    legal_entity_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    status: str | None = None,
) -> MonitorOut:
    return await monitoring.monitor(
        ctx,
        db,
        period_start=period_start,
        legal_entity_id=legal_entity_id,
        client_id=client_id,
        status=status,
    )


async def unapproved_timesheets(
    ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, period_start: date, period_end: date
) -> list[UnapprovedTimesheet]:
    return await monitoring.unapproved_timesheets(ctx, db, legal_entity_id, period_start, period_end)


async def link_summary(ctx: RequestContext, db: AsyncSession, timesheet_id: uuid.UUID) -> object:
    from app.services.time import portal

    return await portal.link_summary(ctx, db, timesheet_id)
