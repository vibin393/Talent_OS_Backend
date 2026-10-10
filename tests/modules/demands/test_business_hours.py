"""Business-hours arithmetic (pure): weekends, holidays, timezone, DST, the Friday -> Tuesday example."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.models.demands import DEFAULT_WORK_WEEK
from app.services.demands.business_hours import (
    Calendar,
    add_business_hours,
    elapsed_business_hours,
    validate_work_week,
)

IST = "Asia/Kolkata"


def local(tz: str, *args: int) -> datetime:
    return datetime(*args, tzinfo=ZoneInfo(tz))  # type: ignore[misc]


def cal(holidays: tuple[date, ...] = ()) -> Calendar:
    return Calendar.from_settings(DEFAULT_WORK_WEEK, holidays)


def test_friday_to_tuesday_over_a_monday_holiday() -> None:
    start = local(IST, 2026, 10, 2, 17, 0)  # Friday 17:00
    due = add_business_hours(start, 8, cal((date(2026, 10, 5),)), IST)  # Monday holiday
    assert due.astimezone(ZoneInfo(IST)) == local(IST, 2026, 10, 6, 16, 0)
    assert due.tzinfo is not None and due.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert elapsed_business_hours(start, due, cal((date(2026, 10, 5),)), IST) == pytest.approx(8)


def test_weekend_and_outside_hours() -> None:
    sat = local(IST, 2026, 10, 3, 11, 0)
    assert add_business_hours(sat, 1, cal(), IST).astimezone(ZoneInfo(IST)) == local(IST, 2026, 10, 5, 10, 0)
    early = local(IST, 2026, 10, 5, 6, 0)
    assert add_business_hours(early, 9, cal(), IST).astimezone(ZoneInfo(IST)) == local(
        IST, 2026, 10, 5, 18, 0
    )
    late = local(IST, 2026, 10, 5, 20, 0)
    assert add_business_hours(late, 0.5, cal(), IST).astimezone(ZoneInfo(IST)) == local(
        IST, 2026, 10, 6, 9, 30
    )
    assert elapsed_business_hours(sat, local(IST, 2026, 10, 4, 23, 0), cal(), IST) == 0
    assert elapsed_business_hours(
        local(IST, 2026, 10, 2, 12, 0), local(IST, 2026, 10, 5, 12, 0), cal(), IST
    ) == pytest.approx(9)


def test_timezone_matters() -> None:
    start = datetime(2026, 10, 5, 3, 30, tzinfo=UTC)  # 09:00 IST, 23:30 previous day in New York
    ist_due = add_business_hours(start, 1, cal(), IST)
    ny_due = add_business_hours(start, 1, cal(), "America/New_York")
    assert ist_due == datetime(2026, 10, 5, 4, 30, tzinfo=UTC)
    assert ny_due.astimezone(ZoneInfo("America/New_York")) == local("America/New_York", 2026, 10, 5, 10, 0)


def test_dst_transition_day() -> None:
    # US DST ends Sunday 2026-11-01; Monday 09:00 EST is 14:00 UTC.
    fri = local("America/New_York", 2026, 10, 30, 17, 0)
    due = add_business_hours(fri, 2, cal(), "America/New_York")
    assert due == datetime(2026, 11, 2, 15, 0, tzinfo=UTC)


def test_calendar_hours_and_validation() -> None:
    with pytest.raises(ValueError, match="working day"):
        validate_work_week({})
    with pytest.raises(ValueError, match="before end"):
        validate_work_week({"mon": {"start": "18:00", "end": "09:00"}})
    with pytest.raises(ValueError, match="unknown weekday"):
        validate_work_week({"funday": {"start": "09:00", "end": "10:00"}})
    validate_work_week(DEFAULT_WORK_WEEK)
