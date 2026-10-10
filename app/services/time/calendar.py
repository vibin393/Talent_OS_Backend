"""Pure working-day and period maths of the ``time`` domain (AD-20: no other module prorates or counts working
days). Days are ``datetime.date``; work weeks are sets of ``date.weekday()`` values (0 = Monday).

Epic 26 (Leave) appends ``leave_days`` here.
"""

import calendar as _calendar
from collections.abc import Iterable
from datetime import date, timedelta
from decimal import Decimal

from app.core.money import MONEY_CONTEXT

DEFAULT_WORK_WEEK: frozenset[int] = frozenset({0, 1, 2, 3, 4})
_FACTOR_QUANTUM = Decimal("0.000001")
# Biweekly periods are anchored on this Monday (shifted to the configured week start).
_BIWEEKLY_ANCHOR = date(2024, 1, 1)


def month_bounds(d: date) -> tuple[date, date]:
    last = _calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, 1), date(d.year, d.month, last)


def period_bounds(kind: str, week_start: int, d: date) -> tuple[date, date]:
    """Return the timesheet period (``weekly | biweekly | semimonthly | monthly``) containing ``d``."""
    if kind == "weekly":
        start = d - timedelta(days=(d.weekday() - week_start) % 7)
        return start, start + timedelta(days=6)
    if kind == "biweekly":
        anchor = _BIWEEKLY_ANCHOR + timedelta(days=(week_start - _BIWEEKLY_ANCHOR.weekday()) % 7)
        offset = (d - anchor).days % 14
        start = d - timedelta(days=offset)
        return start, start + timedelta(days=13)
    if kind == "semimonthly":
        first, last = month_bounds(d)
        if d.day <= 15:
            return first, date(d.year, d.month, 15)
        return date(d.year, d.month, 16), last
    if kind == "monthly":
        return month_bounds(d)
    msg = f"unknown period kind {kind!r}"
    raise ValueError(msg)


def periods_between(kind: str, week_start: int, start: date, end: date) -> list[tuple[date, date]]:
    """Every period overlapping ``[start, end]`` in date order."""
    out: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        bounds = period_bounds(kind, week_start, cursor)
        out.append(bounds)
        cursor = bounds[1] + timedelta(days=1)
    return out


def days_in(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)] if end >= start else []


def working_days(
    start: date, end: date, work_week: Iterable[int] = DEFAULT_WORK_WEEK, holidays: Iterable[date] = ()
) -> int:
    """Working days in ``[start, end]``: days of ``work_week`` minus holidays (each holiday counted once)."""
    week = frozenset(work_week)
    off = frozenset(holidays)
    return sum(1 for d in days_in(start, end) if d.weekday() in week and d not in off)


def month_working_days(
    d: date, work_week: Iterable[int] = DEFAULT_WORK_WEEK, holidays: Iterable[date] = ()
) -> int:
    first, last = month_bounds(d)
    return working_days(first, last, work_week, holidays)


def proration_factor(
    start: date, end: date, work_week: Iterable[int] = DEFAULT_WORK_WEEK, holidays: Iterable[date] = ()
) -> Decimal:
    """Share of the month of ``start`` worked in ``[start, end]`` (clipped to that month), quantized to 6
    dp."""
    first, last = month_bounds(start)
    span_end = min(end, last)
    week = tuple(work_week)
    hol = tuple(holidays)
    total = working_days(first, last, week, hol)
    if total == 0:
        return Decimal("0.000000")
    worked = working_days(max(start, first), span_end, week, hol)
    return MONEY_CONTEXT.divide(Decimal(worked), Decimal(total)).quantize(
        _FACTOR_QUANTUM, context=MONEY_CONTEXT
    )


def business_days_between(start: date, end: date) -> int:
    """Mon-Fri days after ``start`` up to and including ``end`` (0 when ``end <= start``)."""
    if end <= start:
        return 0
    return working_days(start + timedelta(days=1), end)


# ---------------------------------------------------------------- Epic 26: leave day counts
_HALF = Decimal("0.5")


def leave_working_dates(
    start: date, end: date, work_week: Iterable[int] = DEFAULT_WORK_WEEK, holidays: Iterable[date] = ()
) -> list[date]:
    """Return the working days of ``[start, end]`` (work week minus holidays) in date order."""
    week = frozenset(work_week)
    off = frozenset(holidays)
    return [d for d in days_in(start, end) if d.weekday() in week and d not in off]


def leave_days(
    start: date,
    end: date,
    work_week: Iterable[int] = DEFAULT_WORK_WEEK,
    holidays: Iterable[date] = (),
    *,
    start_half: str | None = None,
    end_half: str | None = None,
) -> Decimal:
    """Working days of a leave request (FR-136): working days in ``[start, end]`` minus holidays, minus 0.5
    for
    a ``pm`` start / ``am`` end half day that is itself a working day. A single day with both halves raises
    ``ValueError('both_halves')``. Quantized to 0.01."""
    if end < start:
        msg = "end_before_start"
        raise ValueError(msg)
    if start == end and start_half and end_half:
        msg = "both_halves"
        raise ValueError(msg)
    dates = leave_working_dates(start, end, work_week, holidays)
    total = Decimal(len(dates))
    if start_half == "pm" and start in dates:
        total -= _HALF
    if end_half == "am" and end in dates and not (start == end and start_half):
        total -= _HALF
    return total.quantize(Decimal("0.01"))
