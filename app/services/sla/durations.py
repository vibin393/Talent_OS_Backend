"""Duration arithmetic for escalations and workflow delays (Epic 32), on top of ``core.business_hours`` (the
only duration calculator: business hours come from it; business days walk its ``Calendar``).

Units: ``minutes``, ``hours``, ``days`` (wall clock), ``business_hours`` and ``business_days``. A business day
lands on the same local time of the N-th next working day (clamped into that day's window); a tenant without
working days falls back to wall-clock days (the Phase 1 behaviour of business hours).
"""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.core import business_hours
from app.core.business_hours import Calendar

UNITS = ("minutes", "hours", "days", "business_hours", "business_days")
# Display conversion of escalation policies stored as hours (``after_business_hours``).
BUSINESS_DAY_HOURS = Decimal(8)


def add_business_days(start: datetime, days: int, calendar: Calendar, tz: str) -> datetime:
    cursor = start.astimezone(UTC)
    if days <= 0:
        return cursor
    if not calendar.work_week:
        return cursor + timedelta(days=days)
    zone = ZoneInfo(tz)
    local = cursor.astimezone(zone)
    day, found = local.date(), 0
    for _ in range(3660):
        day += timedelta(days=1)
        window = calendar.window(day)
        if window is None:
            continue
        found += 1
        if found == days:
            at: time = min(max(local.time().replace(tzinfo=None), window[0]), window[1])
            return datetime.combine(day, at, tzinfo=zone).astimezone(UTC)
    return cursor + timedelta(days=days)


def add(start: datetime, value: float | Decimal, unit: str, calendar: Calendar, tz: str) -> datetime:
    """``start`` + ``value`` ``unit`` (aware UTC result)."""
    amount = float(value)
    if unit == "minutes":
        return start.astimezone(UTC) + timedelta(minutes=amount)
    if unit == "hours":
        return business_hours.add_calendar_hours(start, amount)
    if unit == "days":
        return start.astimezone(UTC) + timedelta(days=amount)
    if unit == "business_hours":
        return business_hours.add_business_hours(start, amount, calendar, tz)
    if unit == "business_days":
        return add_business_days(start, int(amount), calendar, tz)
    msg = f"unknown duration unit {unit!r}"
    raise ValueError(msg)


def max_days(value: float, unit: str) -> float:
    """Upper bound of a duration in calendar days (for the ``WORKFLOW_MAX_DELAY_DAYS`` check)."""
    return {
        "minutes": value / 1440,
        "hours": value / 24,
        "days": value,
        "business_hours": value / 24,
        "business_days": value,
    }[unit]


def describe(value: float | Decimal, unit: str) -> str:
    n = float(value)
    shown = int(n) if n == int(n) else n
    label = unit.replace("_", " ")
    if shown == 1:
        label = label[:-1]
    return f"{shown} {label}"
