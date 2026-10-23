"""Adjustment Timesheets (FR-132): signed deltas against an ``approved`` Timesheet. The original stays
untouched;
each approved delta is a new ``adjustment_entry`` posting source. Guard: for every (date, category) the
original
hours + approved deltas + this delta stay >= 0.00, and each day stays within the daily cap."""

import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError
from app.core.events import emit
from app.models.time import Timesheet, TimesheetAdjustmentEntry, TimesheetEntry
from app.schemas.time import TIME_CATEGORIES, AdjustmentIn
from app.services.time.common import DAY_MAX, ZERO, get_timesheet, invalid, parse_hours
from app.services.time.configs import config_version
from app.services.time.events import AdjustmentTimesheetCreated
from app.services.time.timesheets import _new_human_id


async def _approved_deltas(db: AsyncSession, original_id: uuid.UUID) -> dict[tuple[date, str], Decimal]:
    rows = await db.execute(
        select(
            TimesheetAdjustmentEntry.entry_date,
            TimesheetAdjustmentEntry.category,
            TimesheetAdjustmentEntry.delta_hours,
        )
        .join(Timesheet, Timesheet.id == TimesheetAdjustmentEntry.timesheet_id)
        .where(TimesheetAdjustmentEntry.original_timesheet_id == original_id, Timesheet.status == "approved")
    )
    out: dict[tuple[date, str], Decimal] = defaultdict(lambda: ZERO)
    for d, cat, delta in rows.all():
        out[d, cat] += delta
    return out


async def create_adjustment(
    ctx: RequestContext, db: AsyncSession, original_id: uuid.UUID, data: AdjustmentIn
) -> Timesheet:
    del ctx
    original = await get_timesheet(db, original_id, lock=True)
    if original.kind != "regular" or original.status != "approved":
        raise ConflictError("Only an approved timesheet can be adjusted", code="timesheet_not_approved")
    cfg = await config_version(db, original.placement_id, original.config_version)
    base: dict[tuple[date, str], Decimal] = defaultdict(lambda: ZERO)
    for e in (
        await db.execute(select(TimesheetEntry).where(TimesheetEntry.timesheet_id == original.id))
    ).scalars():
        base[e.entry_date, e.category] += e.hours
    approved = await _approved_deltas(db, original.id)
    errors: list[dict[str, object]] = []
    deltas: dict[tuple[date, str], tuple[Decimal, str | None]] = {}
    for line in data.entries:
        delta = parse_hours(line.delta_hours, signed=True)
        key = (line.date, line.category)
        if not original.period_start <= line.date <= original.period_end:
            errors.append(
                {"date": line.date.isoformat(), "category": line.category, "code": "outside_period"}
            )
        elif line.category not in TIME_CATEGORIES:
            errors.append(
                {"date": line.date.isoformat(), "category": line.category, "code": "category_disabled"}
            )
        elif delta is None or delta == 0:
            errors.append({"date": line.date.isoformat(), "category": line.category, "code": "bad_decimal"})
        elif key in deltas:
            errors.append(
                {"date": line.date.isoformat(), "category": line.category, "code": "duplicate_line"}
            )
        else:
            deltas[key] = (delta, line.note)
    if errors:
        raise invalid("entry_invalid", "Some adjustment lines are not valid", rows=errors)
    for key, (delta, _) in deltas.items():  # type: ignore[assignment]
        if base[key] + approved[key] + delta < 0:
            raise invalid(
                "adjustment_below_zero",
                "An adjustment cannot make hours negative",
                date=key[0].isoformat(),
                category=key[1],
            )
    cap = min(Decimal(cfg.daily_cap), DAY_MAX)
    for day in {k[0] for k in deltas}:
        total = sum((v for k, v in base.items() if k[0] == day), ZERO)
        total += sum((v for k, v in approved.items() if k[0] == day), ZERO)
        total += sum((v[0] for k, v in deltas.items() if k[0] == day), ZERO)
        if total > cap:
            raise invalid(
                "entry_invalid",
                "A day would exceed the daily cap",
                rows=[{"date": day.isoformat(), "code": "over_daily_cap"}],
            )
    seq = (
        await db.execute(
            select(func.coalesce(func.max(Timesheet.adjustment_seq), 0)).where(
                Timesheet.original_timesheet_id == original.id
            )
        )
    ).scalar_one()
    adj = Timesheet(
        id=uuid.uuid4(),
        tenant_id=original.tenant_id,
        human_id=await _new_human_id(db),
        placement_id=original.placement_id,
        candidate_id=original.candidate_id,
        client_id=original.client_id,
        vendor_id=original.vendor_id,
        legal_entity_id=original.legal_entity_id,
        kind="adjustment",
        adjustment_seq=int(seq) + 1,
        original_timesheet_id=original.id,
        period_start=original.period_start,
        period_end=original.period_end,
        status="draft",
        config_version=original.config_version,
        reason=data.reason,
    )
    db.add(adj)
    await db.flush()
    for (d, cat), (delta, note) in deltas.items():
        db.add(
            TimesheetAdjustmentEntry(
                id=uuid.uuid4(),
                tenant_id=adj.tenant_id,
                timesheet_id=adj.id,
                original_timesheet_id=original.id,
                entry_date=d,
                category=cat,
                delta_hours=delta,
                note=note,
            )
        )
    await db.flush()
    emit(
        db,
        AdjustmentTimesheetCreated(
            tenant_id=adj.tenant_id,
            timesheet_id=adj.id,
            original_timesheet_id=original.id,
            placement_id=adj.placement_id,
        ),
    )
    return adj
