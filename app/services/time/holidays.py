"""Holiday Calendars (Epic 26, FR-134).

* CRUD (``holiday:manage``); delete only when unassigned (no timesheet config / placement references it), else
  archive. Holiday edits emit ``HolidayCalendarChanged`` (draft Timesheets of affected placements re-prefill;
  submitted ones are never touched).
* ``copy_calendar`` shifts every holiday to the target year; Feb 29 to a non-leap year is skipped (listed in
  ``skipped``); copied ``national`` / ``state`` rows are flagged ``needs_review`` (movable festivals).
* Resolution ``resolve_calendar(placement, year)``: explicit ``timesheet_configs.holiday_calendar_id`` (else
  ``placements.holiday_calendar_id``) -- for another year the same-scope calendar of that year -- otherwise
  most
  specific wins: ``client`` -> ``legal_entity`` -> ``state`` (work-location state) -> ``country`` (legal
  entity
  country); ties -> oldest.
* ``holidays_for`` merges the resolved calendar's national / state / client holidays with the Worker's
  selected
  optional holidays.
"""

import dataclasses
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.models.legal_entities import LegalEntity
from app.models.placements import Placement
from app.models.time import (
    Holiday,
    HolidayCalendar,
    LeaveSettings,
    Timesheet,
    TimesheetConfig,
    WorkerOptionalHoliday,
)
from app.schemas.leave import (
    CopiedHoliday,
    CopyCalendarOut,
    HolidayCalendarDetail,
    HolidayCalendarIn,
    HolidayCalendarOut,
    HolidayCalendarUpdate,
    HolidayDayOut,
    HolidayIn,
    HolidayOut,
    HolidayUpdate,
    PlacementHolidaysOut,
)
from app.schemas.time import PrefillItem
from app.services.time import calendar
from app.services.time.common import get_placement, now, tenant_of
from app.services.time.leave_events import HolidayCalendarChanged

COUNTED_TYPES = ("national", "state", "client")
REVIEW_TYPES = ("national", "state")


@dataclasses.dataclass(frozen=True, slots=True)
class HolidayDay:
    date: date
    name: str
    type: str
    holiday_id: uuid.UUID
    calendar_id: uuid.UUID
    optional: bool = False
    selected: bool = False


# ---------------------------------------------------------------- settings
async def leave_settings_row(db: AsyncSession, tenant_id: uuid.UUID) -> LeaveSettings:
    row = await db.get(LeaveSettings, tenant_id)
    if row is None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        # concurrent first use: never a PK violation
        await db.execute(pg_insert(LeaveSettings).values(tenant_id=tenant_id).on_conflict_do_nothing())
        row = (
            await db.execute(select(LeaveSettings).where(LeaveSettings.tenant_id == tenant_id))
        ).scalar_one()
    return row


# ---------------------------------------------------------------- CRUD
def _cal_out(c: HolidayCalendar, count: int = 0) -> HolidayCalendarOut:
    return HolidayCalendarOut(
        id=c.id,
        name=c.name,
        year=c.year,
        scope_kind=c.scope_kind,
        country_code=c.country_code,
        state_code=c.state_code,
        client_id=c.client_id,
        legal_entity_id=c.legal_entity_id,
        optional_allowance=c.optional_allowance,
        copied_from_id=c.copied_from_id,
        archived_at=c.archived_at,
        holiday_count=count,
    )


def _holiday_out(h: Holiday) -> HolidayOut:
    return HolidayOut(
        id=h.id,
        calendar_id=h.calendar_id,
        holiday_date=h.holiday_date,
        name=h.name,
        type=h.type,
    )


async def _get(db: AsyncSession, calendar_id: uuid.UUID, *, lock: bool = False) -> HolidayCalendar:
    stmt = select(HolidayCalendar).where(HolidayCalendar.id == calendar_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Holiday calendar not found")
    return row


async def _holidays(db: AsyncSession, calendar_id: uuid.UUID) -> list[Holiday]:
    return list(
        (
            await db.execute(
                select(Holiday)
                .where(Holiday.calendar_id == calendar_id)
                .order_by(Holiday.holiday_date, Holiday.name)
            )
        ).scalars()
    )


async def list_calendars(
    ctx: RequestContext, db: AsyncSession, *, year: int | None = None, include_archived: bool = False
) -> list[HolidayCalendarOut]:
    del ctx
    counts = select(Holiday.calendar_id, func.count().label("n")).group_by(Holiday.calendar_id).subquery()
    stmt = select(HolidayCalendar, func.coalesce(counts.c.n, 0)).outerjoin(
        counts, counts.c.calendar_id == HolidayCalendar.id
    )
    if year is not None:
        stmt = stmt.where(HolidayCalendar.year == year)
    if not include_archived:
        stmt = stmt.where(HolidayCalendar.archived_at.is_(None))
    rows = (await db.execute(stmt.order_by(HolidayCalendar.year.desc(), HolidayCalendar.name))).all()
    return [_cal_out(c, int(n)) for c, n in rows]


async def get_calendar(
    ctx: RequestContext, db: AsyncSession, calendar_id: uuid.UUID
) -> HolidayCalendarDetail:
    del ctx
    c = await _get(db, calendar_id)
    hols = await _holidays(db, c.id)
    return HolidayCalendarDetail(
        **_cal_out(c, len(hols)).model_dump(), holidays=[_holiday_out(h) for h in hols]
    )


def _validate_scope(data: HolidayCalendarIn) -> dict[str, Any]:
    values: dict[str, Any] = {
        "country_code": data.country_code,
        "state_code": None,
        "client_id": None,
        "legal_entity_id": None,
    }
    if data.scope_kind == "state":
        if not data.state_code:
            raise ValidationFailed(
                "A state calendar needs a state", code="scope_ref_required", details={"field": "state_code"}
            )
        values["state_code"] = data.state_code
    elif data.scope_kind == "client":
        if data.client_id is None:
            raise ValidationFailed(
                "A Client calendar needs a Client", code="scope_ref_required", details={"field": "client_id"}
            )
        values["client_id"] = data.client_id
    elif data.scope_kind == "legal_entity":
        if data.legal_entity_id is None:
            raise ValidationFailed(
                "A Legal Entity calendar needs a Legal Entity",
                code="scope_ref_required",
                details={"field": "legal_entity_id"},
            )
        values["legal_entity_id"] = data.legal_entity_id
    return values


async def _scope_taken(
    db: AsyncSession, c: HolidayCalendar | dict[str, Any], year: int, scope_kind: str
) -> bool:
    get = (lambda k: getattr(c, k)) if isinstance(c, HolidayCalendar) else c.get
    stmt = select(HolidayCalendar.id).where(
        HolidayCalendar.scope_kind == scope_kind,
        HolidayCalendar.year == year,
        HolidayCalendar.archived_at.is_(None),
        HolidayCalendar.country_code == get("country_code"),
    )
    for col in ("state_code", "client_id", "legal_entity_id"):
        value = get(col)
        column = getattr(HolidayCalendar, col)
        stmt = stmt.where(column.is_(None) if value is None else column == value)
    return (await db.execute(stmt.limit(1))).scalar_one_or_none() is not None


def _duplicate() -> ConflictError:
    return ConflictError("A calendar for this scope and year already exists", code="holiday_calendar_exists")


def _check_holiday_year(cal: HolidayCalendar, d: date) -> None:
    if d.year != cal.year:
        raise ValidationFailed(
            "The holiday date must be in the calendar's year",
            code="holiday_wrong_year",
            details={"year": cal.year},
        )


async def create_calendar(
    ctx: RequestContext, db: AsyncSession, data: HolidayCalendarIn
) -> HolidayCalendarDetail:
    scope = _validate_scope(data)
    if await _scope_taken(db, scope, data.year, data.scope_kind):
        raise _duplicate()
    cal = HolidayCalendar(
        id=uuid.uuid4(),
        tenant_id=tenant_of(ctx, db),
        name=data.name,
        year=data.year,
        scope_kind=data.scope_kind,
        optional_allowance=data.optional_allowance,
        **scope,
    )
    db.add(cal)
    await db.flush()
    seen: set[tuple[date, str]] = set()
    for h in data.holidays:
        _check_holiday_year(cal, h.holiday_date)
        if (h.holiday_date, h.name) in seen:
            continue
        seen.add((h.holiday_date, h.name))
        db.add(
            Holiday(
                id=uuid.uuid4(),
                tenant_id=cal.tenant_id,
                calendar_id=cal.id,
                holiday_date=h.holiday_date,
                name=h.name,
                type=h.type,
            )
        )
    await db.flush()
    emit(db, HolidayCalendarChanged(tenant_id=cal.tenant_id, calendar_id=cal.id))
    return await get_calendar(ctx, db, cal.id)


async def update_calendar(
    ctx: RequestContext, db: AsyncSession, calendar_id: uuid.UUID, data: HolidayCalendarUpdate
) -> HolidayCalendarDetail:
    cal = await _get(db, calendar_id, lock=True)
    if data.name is not None:
        cal.name = data.name
    if data.optional_allowance is not None:
        cal.optional_allowance = data.optional_allowance
    await db.flush()
    return await get_calendar(ctx, db, cal.id)


async def _assigned(db: AsyncSession, calendar_id: uuid.UUID) -> bool:
    cfg = (
        await db.execute(
            select(TimesheetConfig.id).where(TimesheetConfig.holiday_calendar_id == calendar_id).limit(1)
        )
    ).scalar_one_or_none()
    if cfg is not None:
        return True
    pl = (
        await db.execute(select(Placement.id).where(Placement.holiday_calendar_id == calendar_id).limit(1))
    ).scalar_one_or_none()
    return pl is not None


async def delete_calendar(ctx: RequestContext, db: AsyncSession, calendar_id: uuid.UUID) -> dict[str, str]:
    del ctx
    cal = await _get(db, calendar_id, lock=True)
    tenant_id = cal.tenant_id
    if await _assigned(db, cal.id):
        if cal.archived_at is None:
            cal.archived_at = now()
            await db.flush()
        emit(db, HolidayCalendarChanged(tenant_id=tenant_id, calendar_id=calendar_id))
        return {"status": "archived"}
    await db.delete(cal)
    await db.flush()
    emit(db, HolidayCalendarChanged(tenant_id=tenant_id, calendar_id=calendar_id))
    return {"status": "deleted"}


async def add_holiday(
    ctx: RequestContext, db: AsyncSession, calendar_id: uuid.UUID, data: HolidayIn
) -> HolidayOut:
    del ctx
    cal = await _get(db, calendar_id, lock=True)
    _check_holiday_year(cal, data.holiday_date)
    exists = (
        await db.execute(
            select(Holiday.id).where(
                Holiday.calendar_id == cal.id,
                Holiday.holiday_date == data.holiday_date,
                Holiday.name == data.name,
            )
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise ConflictError("This holiday already exists", code="holiday_exists")
    h = Holiday(
        id=uuid.uuid4(),
        tenant_id=cal.tenant_id,
        calendar_id=cal.id,
        holiday_date=data.holiday_date,
        name=data.name,
        type=data.type,
    )
    db.add(h)
    await db.flush()
    emit(db, HolidayCalendarChanged(tenant_id=cal.tenant_id, calendar_id=cal.id))
    return _holiday_out(h)


async def _get_holiday(db: AsyncSession, holiday_id: uuid.UUID) -> Holiday:
    h = (await db.execute(select(Holiday).where(Holiday.id == holiday_id))).scalar_one_or_none()
    if h is None:
        raise NotFound("Holiday not found")
    return h


async def update_holiday(
    ctx: RequestContext, db: AsyncSession, holiday_id: uuid.UUID, data: HolidayUpdate
) -> HolidayOut:
    del ctx
    h = await _get_holiday(db, holiday_id)
    cal = await _get(db, h.calendar_id, lock=True)
    if data.holiday_date is not None:
        _check_holiday_year(cal, data.holiday_date)
        h.holiday_date = data.holiday_date
    if data.name is not None:
        h.name = data.name
    if data.type is not None:
        h.type = data.type
    await db.flush()
    emit(db, HolidayCalendarChanged(tenant_id=cal.tenant_id, calendar_id=cal.id))
    return _holiday_out(h)


async def delete_holiday(ctx: RequestContext, db: AsyncSession, holiday_id: uuid.UUID) -> None:
    del ctx
    h = await _get_holiday(db, holiday_id)
    tenant_id, calendar_id = h.tenant_id, h.calendar_id
    await db.delete(h)
    await db.flush()
    emit(db, HolidayCalendarChanged(tenant_id=tenant_id, calendar_id=calendar_id))


def _shift(d: date, year: int) -> date | None:
    try:
        return d.replace(year=year)
    except ValueError:  # Feb 29 -> non-leap year
        return None


async def copy_calendar(
    ctx: RequestContext, db: AsyncSession, calendar_id: uuid.UUID, year: int
) -> CopyCalendarOut:
    del ctx
    src = await _get(db, calendar_id)
    if year == src.year:
        raise ValidationFailed("Choose another year to copy to", code="copy_same_year")
    if await _scope_taken(db, src, year, src.scope_kind):
        raise _duplicate()
    new = HolidayCalendar(
        id=uuid.uuid4(),
        tenant_id=src.tenant_id,
        name=src.name.replace(str(src.year), str(year)) if str(src.year) in src.name else src.name,
        year=year,
        scope_kind=src.scope_kind,
        country_code=src.country_code,
        state_code=src.state_code,
        client_id=src.client_id,
        legal_entity_id=src.legal_entity_id,
        optional_allowance=src.optional_allowance,
        copied_from_id=src.id,
    )
    db.add(new)
    await db.flush()
    copied: list[CopiedHoliday] = []
    skipped = []
    from app.schemas.leave import SkippedHoliday

    for h in await _holidays(db, src.id):
        target = _shift(h.holiday_date, year)
        if target is None:
            skipped.append(
                SkippedHoliday(holiday_date=h.holiday_date, name=h.name, reason="feb_29_non_leap_year")
            )
            continue
        row = Holiday(
            id=uuid.uuid4(),
            tenant_id=new.tenant_id,
            calendar_id=new.id,
            holiday_date=target,
            name=h.name,
            type=h.type,
        )
        db.add(row)
        copied.append(
            CopiedHoliday(
                id=row.id,
                holiday_date=target,
                name=h.name,
                type=h.type,
                needs_review=h.type in REVIEW_TYPES,
            )
        )
    await db.flush()
    emit(db, HolidayCalendarChanged(tenant_id=new.tenant_id, calendar_id=new.id))
    return CopyCalendarOut(calendar=_cal_out(new, len(copied)), copied=copied, skipped=skipped)


# ---------------------------------------------------------------- resolution
async def _country_of(db: AsyncSession, legal_entity_id: uuid.UUID) -> str:
    row = (
        await db.execute(select(LegalEntity.country).where(LegalEntity.id == legal_entity_id))
    ).scalar_one_or_none()
    return str(row or "IN")


async def _explicit_calendar_id(db: AsyncSession, placement: Placement) -> uuid.UUID | None:
    from app.services.time.configs import latest_config

    cfg = await latest_config(db, placement.id)
    if cfg is not None and cfg.holiday_calendar_id is not None:
        return cfg.holiday_calendar_id
    return placement.holiday_calendar_id


async def _same_scope(db: AsyncSession, cal: HolidayCalendar, year: int) -> uuid.UUID | None:
    stmt = select(HolidayCalendar.id).where(
        HolidayCalendar.scope_kind == cal.scope_kind,
        HolidayCalendar.year == year,
        HolidayCalendar.archived_at.is_(None),
        HolidayCalendar.country_code == cal.country_code,
    )
    for col in ("state_code", "client_id", "legal_entity_id"):
        value = getattr(cal, col)
        column = getattr(HolidayCalendar, col)
        stmt = stmt.where(column.is_(None) if value is None else column == value)
    return (await db.execute(stmt.order_by(HolidayCalendar.created_at).limit(1))).scalar_one_or_none()


async def resolve_calendar(db: AsyncSession, placement: Placement, year: int) -> uuid.UUID | None:
    explicit = await _explicit_calendar_id(db, placement)
    if explicit is not None:
        cal = (
            await db.execute(select(HolidayCalendar).where(HolidayCalendar.id == explicit))
        ).scalar_one_or_none()
        if cal is not None:
            if cal.year == year:
                return cal.id
            same = await _same_scope(db, cal, year)
            if same is not None:
                return same
    base = select(HolidayCalendar.id).where(
        HolidayCalendar.year == year, HolidayCalendar.archived_at.is_(None)
    )
    country = await _country_of(db, placement.legal_entity_id)
    candidates = [
        base.where(HolidayCalendar.scope_kind == "client", HolidayCalendar.client_id == placement.client_id),
        base.where(
            HolidayCalendar.scope_kind == "legal_entity",
            HolidayCalendar.legal_entity_id == placement.legal_entity_id,
        ),
    ]
    if placement.work_state_code:
        candidates.append(
            base.where(
                HolidayCalendar.scope_kind == "state", HolidayCalendar.state_code == placement.work_state_code
            )
        )
    candidates.append(
        base.where(HolidayCalendar.scope_kind == "country", HolidayCalendar.country_code == country)
    )
    for stmt in candidates:
        found = (await db.execute(stmt.order_by(HolidayCalendar.created_at).limit(1))).scalar_one_or_none()
        if found is not None:
            return found
    return None


async def default_calendar_for(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, year: int
) -> uuid.UUID | None:
    """Return the default calendar of a placement for ``year`` (used by Timesheets when creating a config)."""
    del ctx
    return await resolve_calendar(db, await get_placement(db, placement_id), year)


async def holidays_for(db: AsyncSession, placement_id: uuid.UUID, start: date, end: date) -> list[HolidayDay]:
    """Return the counted holidays (national / state / client of the resolved calendars, plus the
    Worker's selected optional holidays) in ``[start, end]``."""
    return [d for d in await _all_days(db, placement_id, start, end) if not d.optional or d.selected]


async def _all_days(db: AsyncSession, placement_id: uuid.UUID, start: date, end: date) -> list[HolidayDay]:
    if end < start:
        return []
    placement = await get_placement(db, placement_id)
    cal_ids: list[uuid.UUID] = []
    for year in range(start.year, end.year + 1):
        cid = await resolve_calendar(db, placement, year)
        if cid is not None:
            cal_ids.append(cid)
    if not cal_ids:
        return []
    rows = list(
        (
            await db.execute(
                select(Holiday)
                .where(
                    Holiday.calendar_id.in_(cal_ids),
                    Holiday.holiday_date >= start,
                    Holiday.holiday_date <= end,
                )
                .order_by(Holiday.holiday_date, Holiday.name)
            )
        ).scalars()
    )
    picked = set(
        (
            await db.execute(
                select(WorkerOptionalHoliday.holiday_id).where(
                    WorkerOptionalHoliday.placement_id == placement_id
                )
            )
        ).scalars()
    )
    return [
        HolidayDay(
            date=h.holiday_date,
            name=h.name,
            type=h.type,
            holiday_id=h.id,
            calendar_id=h.calendar_id,
            optional=h.type == "optional",
            selected=h.id in picked,
        )
        for h in rows
    ]


def day_out(d: HolidayDay) -> HolidayDayOut:
    return HolidayDayOut(
        date=d.date,
        name=d.name,
        type=d.type,
        holiday_id=d.holiday_id,
        calendar_id=d.calendar_id,
        optional=d.optional,
        selected=d.selected,
    )


async def placement_holidays(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, start: date, end: date
) -> PlacementHolidaysOut:
    del ctx
    if (end - start).days > 731:
        raise ValidationFailed("Choose a range of at most two years", code="range_too_long")
    placement = await get_placement(db, placement_id)
    days = await _all_days(db, placement.id, start, end)
    ids = sorted({d.calendar_id for d in days}, key=str)
    return PlacementHolidaysOut(
        placement_id=placement.id, calendar_ids=ids, holidays=[day_out(d) for d in days]
    )


# ---------------------------------------------------------------- optional holidays (Worker)
async def set_optional(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    placement_id: uuid.UUID,
    holiday_id: uuid.UUID,
    *,
    selected: bool,
) -> None:
    """Select / unselect an optional holiday. Selections count against the calendar's ``optional_allowance``
    (409 ``optional_allowance_exceeded``) and only before the Timesheet covering the date is submitted (409
    ``timesheet_submitted``)."""
    from app.services.time.monitoring import timesheet_state_for

    placement = await get_placement(db, placement_id)
    if placement.candidate_id != candidate_id:
        raise NotFound("Placement not found")
    h = await _get_holiday(db, holiday_id)
    if h.type != "optional":
        raise ValidationFailed("Only optional holidays can be selected", code="not_optional_holiday")
    cal = await _get(db, h.calendar_id, lock=True)
    if await resolve_calendar(db, placement, h.holiday_date.year) != cal.id:
        raise NotFound("Holiday not found")
    state = await timesheet_state_for(ctx, db, placement.id, h.holiday_date)
    if state is not None and state.status not in ("draft", "rejected"):
        raise ConflictError(
            "The timesheet for this date is already submitted",
            code="timesheet_submitted",
            details={"status": state.status},
        )
    existing = (
        await db.execute(
            select(WorkerOptionalHoliday).where(
                WorkerOptionalHoliday.placement_id == placement.id, WorkerOptionalHoliday.holiday_id == h.id
            )
        )
    ).scalar_one_or_none()
    if not selected:
        if existing is not None:
            await db.delete(existing)
            await db.flush()
    else:
        if existing is not None:
            return
        taken = (
            await db.execute(
                select(func.count())
                .select_from(WorkerOptionalHoliday)
                .join(Holiday, Holiday.id == WorkerOptionalHoliday.holiday_id)
                .where(WorkerOptionalHoliday.placement_id == placement.id, Holiday.calendar_id == cal.id)
            )
        ).scalar_one()
        if int(taken) >= cal.optional_allowance:
            raise ConflictError(
                "You have used all your optional holidays for this year",
                code="optional_allowance_exceeded",
                details={"allowance": cal.optional_allowance},
            )
        db.add(
            WorkerOptionalHoliday(
                id=uuid.uuid4(),
                tenant_id=placement.tenant_id,
                candidate_id=candidate_id,
                placement_id=placement.id,
                holiday_id=h.id,
            )
        )
        await db.flush()
    from app.services.time.leave_requests import refresh_drafts

    await refresh_drafts(ctx, db, placement.id, h.holiday_date, h.holiday_date)


async def optional_allowance_for(db: AsyncSession, placement: Placement, year: int) -> tuple[int, int]:
    cid = await resolve_calendar(db, placement, year)
    if cid is None:
        return 0, 0
    cal = await _get(db, cid)
    taken = (
        await db.execute(
            select(func.count())
            .select_from(WorkerOptionalHoliday)
            .join(Holiday, Holiday.id == WorkerOptionalHoliday.holiday_id)
            .where(WorkerOptionalHoliday.placement_id == placement.id, Holiday.calendar_id == cid)
        )
    ).scalar_one()
    return cal.optional_allowance, int(taken)


# ---------------------------------------------------------------- Timesheet prefill source (A6)
async def holiday_prefill(
    db: AsyncSession, placement_id: uuid.UUID, start: date, end: date
) -> list[PrefillItem]:
    from app.services.time.configs import latest_config

    cfg = await latest_config(db, placement_id)
    hours = Decimal(cfg.hours_per_day) if cfg is not None and cfg.hours_per_day else Decimal("8.00")
    week = frozenset(cfg.work_days) if cfg is not None and cfg.work_days else calendar.DEFAULT_WORK_WEEK
    out: list[PrefillItem] = []
    seen: set[date] = set()
    for d in await holidays_for(db, placement_id, start, end):
        if d.date.weekday() not in week or d.date in seen:
            continue
        seen.add(d.date)
        out.append(
            PrefillItem(
                date=d.date,
                category="holiday",
                hours=hours.quantize(Decimal("0.01")),
                holiday_id=d.holiday_id,
                label=d.name,
            )
        )
    return out


async def placements_using(db: AsyncSession, calendar_id: uuid.UUID) -> list[tuple[uuid.UUID, date, date]]:
    """Placements with a draft regular Timesheet in the calendar's year whose resolved calendar is
    ``calendar_id``
    (holiday-change refresh). Returns ``(placement_id, period_start, period_end)`` of each such draft."""
    cal = (
        await db.execute(select(HolidayCalendar).where(HolidayCalendar.id == calendar_id))
    ).scalar_one_or_none()
    if cal is None:
        return []
    first, last = date(cal.year, 1, 1), date(cal.year, 12, 31)
    drafts = (
        await db.execute(
            select(Timesheet.placement_id, Timesheet.period_start, Timesheet.period_end).where(
                Timesheet.kind == "regular",
                Timesheet.status.in_(("draft", "rejected")),
                Timesheet.period_end >= first,
                Timesheet.period_start <= last + timedelta(days=0),
            )
        )
    ).all()
    out: list[tuple[uuid.UUID, date, date]] = []
    cache: dict[uuid.UUID, uuid.UUID | None] = {}
    for pid, ps, pe in drafts:
        if pid not in cache:
            placement = (await db.execute(select(Placement).where(Placement.id == pid))).scalar_one_or_none()
            cache[pid] = await resolve_calendar(db, placement, cal.year) if placement is not None else None
        if cache[pid] == calendar_id or cal.archived_at is not None:
            out.append((pid, ps, pe))
    return out
