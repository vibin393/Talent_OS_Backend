"""Business-hours arithmetic for SLA clocks (story 4.6; promoted to ``core`` by Epic 13, AD-31).

The arithmetic is pure (no I/O); ``tenant_calendar(db)`` reads the tenant's work week / holidays
(``demand_settings``) and timezone (``tenants``) through lightweight ``sqlalchemy.table()`` constructs, so
``core`` never imports a service module.

A ``Calendar`` is the tenant's work week (``{"mon": {"start": "09:00", "end": "18:00"}, ...}``; missing
days are non-working) plus holiday dates, interpreted in the tenant timezone. Arithmetic is done on UTC
instants so DST changes are handled correctly.
"""

import dataclasses
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MAX_DAYS = 3660  # safety net: ~10 years of calendar days


def _parse_time(value: str) -> time:
    if value == "24:00":  # end of day
        return time.max
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


@dataclasses.dataclass(frozen=True, slots=True)
class Calendar:
    work_week: Mapping[str, tuple[time, time]]
    holidays: frozenset[date] = frozenset()

    @classmethod
    def from_settings(cls, work_week: Mapping[str, Any], holidays: Iterable[date]) -> "Calendar":
        week: dict[str, tuple[time, time]] = {}
        for day, window in (work_week or {}).items():
            if day in WEEKDAYS and window:
                start, end = _parse_time(window["start"]), _parse_time(window["end"])
                if start < end:
                    week[day] = (start, end)
        return cls(week, frozenset(holidays or ()))

    def window(self, day: date) -> tuple[time, time] | None:
        if day in self.holidays:
            return None
        return self.work_week.get(WEEKDAYS[day.weekday()])


def validate_work_week(work_week: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` unless every entry is a known weekday with ``start < end`` (HH:MM) and at
    least one day works."""
    if not work_week:
        raise ValueError("at least one working day is required")
    for day, window in work_week.items():
        if day not in WEEKDAYS:
            raise ValueError(f"unknown weekday {day!r}")
        try:
            start, end = _parse_time(window["start"]), _parse_time(window["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{day}: start/end must be HH:MM") from exc
        if not start < end:
            raise ValueError(f"{day}: start must be before end")


def _bounds(day: date, window: tuple[time, time], zone: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, window[0], tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day, window[1], tzinfo=zone).astimezone(UTC)
    return start, end


def add_business_hours(start: datetime, hours: float, calendar: Calendar, tz: str) -> datetime:
    """Return the instant ``hours`` business hours after ``start`` (aware UTC result)."""
    cursor = start.astimezone(UTC)
    remaining = timedelta(hours=float(hours))
    if remaining <= timedelta(0):
        return cursor
    zone = ZoneInfo(tz)
    day = cursor.astimezone(zone).date()
    for _ in range(_MAX_DAYS):
        window = calendar.window(day)
        if window is not None:
            day_start, day_end = _bounds(day, window, zone)
            seg_start = max(cursor, day_start)
            if seg_start < day_end:
                available = day_end - seg_start
                if remaining <= available:
                    return seg_start + remaining
                remaining -= available
        day += timedelta(days=1)
    return start.astimezone(UTC) + timedelta(hours=float(hours))  # no working days configured


def elapsed_business_hours(start: datetime, end: datetime, calendar: Calendar, tz: str) -> float:
    """Return the business hours between ``start`` and ``end`` (0 when ``end <= start``)."""
    a, b = start.astimezone(UTC), end.astimezone(UTC)
    if b <= a:
        return 0.0
    zone = ZoneInfo(tz)
    day, last = a.astimezone(zone).date(), b.astimezone(zone).date()
    total = timedelta(0)
    for _ in range(_MAX_DAYS):
        if day > last:
            break
        window = calendar.window(day)
        if window is not None:
            day_start, day_end = _bounds(day, window, zone)
            lo, hi = max(a, day_start), min(b, day_end)
            if lo < hi:
                total += hi - lo
        day += timedelta(days=1)
    return total.total_seconds() / 3600


def add_calendar_hours(start: datetime, hours: float) -> datetime:
    return start.astimezone(UTC) + timedelta(hours=float(hours))


def elapsed_calendar_hours(start: datetime, end: datetime) -> float:
    return max((end - start).total_seconds() / 3600, 0.0)


_demand_settings = table("demand_settings", column("work_week"), column("holidays"))
_tenants = table("tenants", column("id"), column("timezone"))


async def tenant_calendar(db: AsyncSession) -> tuple[Calendar, str]:
    """The session tenant's business calendar and timezone (a tenant without settings has no working days,
    so business-hour arithmetic falls back to calendar hours -- the Phase 1 behaviour)."""
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "tenant_calendar needs a tenant session"
        raise RuntimeError(msg)
    row = (await db.execute(select(_demand_settings.c.work_week, _demand_settings.c.holidays))).first()
    tz = (
        await db.execute(select(_tenants.c.timezone).where(_tenants.c.id == uuid.UUID(str(raw))))
    ).scalar_one_or_none()
    work_week = row.work_week if row is not None else None
    holidays = row.holidays if row is not None else None
    return Calendar.from_settings(work_week or {}, holidays or []), tz or "UTC"
