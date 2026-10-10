"""Business-date and Indian financial-year derivation (AD-14, AD-17). Pure: no I/O.

Instants are aware datetimes; business dates are derived once with the Legal Entity's (or work location's)
timezone. The financial year runs April-March and is written ``"26-27"`` (2026-04-01 .. 2027-03-31).
"""

import calendar
import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

_FY_RE = re.compile(r"^(\d{2})-(\d{2})$")


def local_date(instant: datetime, tz: str) -> date:
    """The calendar date of ``instant`` in ``tz``. Naive datetimes are refused (``ValueError``)."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        msg = "local_date needs an aware datetime"
        raise ValueError(msg)
    return instant.astimezone(ZoneInfo(tz)).date()


def today_for(tz: str, now: datetime | None = None) -> date:
    return local_date(now or datetime.now(UTC), tz)


def fiscal_year(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


def fiscal_year_bounds(fy: str) -> tuple[date, date]:
    match = _FY_RE.match(fy or "")
    if match is None:
        msg = f"invalid financial year {fy!r} (expected 'YY-YY')"
        raise ValueError(msg)
    first, second = int(match.group(1)), int(match.group(2))
    if (first + 1) % 100 != second:
        msg = f"invalid financial year {fy!r} (years must be consecutive)"
        raise ValueError(msg)
    start_year = 2000 + first
    return date(start_year, 4, 1), date(start_year + 1, 3, 31)


def month_key(d: date) -> tuple[int, int]:
    return d.year, d.month


def month_bounds(year: int, month: int) -> tuple[date, date]:
    if not 1 <= month <= 12:
        msg = f"invalid month {month}"
        raise ValueError(msg)
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """``(year, month)`` shifted by ``delta`` months."""
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1
