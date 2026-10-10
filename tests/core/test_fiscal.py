"""AD-14 / AD-17: business dates and Indian financial years (Epic 13, story 13.1)."""

from datetime import UTC, date, datetime

import pytest

from app.core.fiscal import (
    add_months,
    fiscal_year,
    fiscal_year_bounds,
    local_date,
    month_bounds,
    month_key,
    today_for,
)


def test_fiscal_year_boundaries() -> None:
    assert fiscal_year(date(2026, 4, 1)) == "26-27"
    assert fiscal_year(date(2027, 3, 31)) == "26-27"
    assert fiscal_year(date(2027, 4, 1)) == "27-28"
    assert fiscal_year(date(2099, 6, 1)) == "99-00"


def test_fiscal_year_bounds() -> None:
    assert fiscal_year_bounds("26-27") == (date(2026, 4, 1), date(2027, 3, 31))
    for bad in ("2026-27", "26-28", "x"):
        with pytest.raises(ValueError):
            fiscal_year_bounds(bad)


def test_local_date_near_midnight_ist() -> None:
    assert local_date(datetime(2027, 3, 31, 18, 45, tzinfo=UTC), "Asia/Kolkata") == date(2027, 4, 1)
    assert local_date(datetime(2027, 3, 31, 18, 15, tzinfo=UTC), "Asia/Kolkata") == date(2027, 3, 31)


def test_naive_datetime_refused() -> None:
    with pytest.raises(ValueError):
        local_date(datetime(2027, 3, 31, 18, 45), "Asia/Kolkata")


def test_today_and_months() -> None:
    assert today_for("Asia/Kolkata", datetime(2026, 12, 31, 20, 0, tzinfo=UTC)) == date(2027, 1, 1)
    assert month_key(date(2026, 5, 9)) == (2026, 5)
    assert month_bounds(2028, 2) == (date(2028, 2, 1), date(2028, 2, 29))
    assert add_months(2026, 11, 3) == (2027, 2)
    assert add_months(2026, 1, -1) == (2025, 12)
    with pytest.raises(ValueError):
        month_bounds(2026, 13)
