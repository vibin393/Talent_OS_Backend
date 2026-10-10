"""AD-14 time conversion (pure): IST / UTC / New York, DST gap, ambiguous fold, display strings."""

from datetime import UTC, datetime

import pytest

from app.core.errors import ValidationFailed
from app.services.interviews.timeutil import local_iso, render_for, to_utc


def test_ist_to_utc() -> None:
    assert to_utc("2026-10-06T10:30", "Asia/Kolkata") == datetime(2026, 10, 6, 5, 0, tzinfo=UTC)


def test_utc_identity() -> None:
    assert to_utc("2026-10-06T10:30", "UTC") == datetime(2026, 10, 6, 10, 30, tzinfo=UTC)


def test_new_york_summer_and_winter() -> None:
    assert to_utc("2026-07-01T09:00", "America/New_York") == datetime(2026, 7, 1, 13, 0, tzinfo=UTC)
    assert to_utc("2026-12-01T09:00", "America/New_York") == datetime(2026, 12, 1, 14, 0, tzinfo=UTC)


def test_dst_gap_is_rejected() -> None:
    with pytest.raises(ValidationFailed) as err:
        to_utc("2026-03-08T02:30", "America/New_York")
    assert err.value.code == "nonexistent_local_time"


def test_ambiguous_time_uses_first_occurrence() -> None:
    # 01:30 happens twice on 1 Nov 2026 in New York; fold=0 is the earlier (EDT, UTC-4) one.
    assert to_utc("2026-11-01T01:30", "America/New_York") == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


def test_invalid_timezone() -> None:
    with pytest.raises(ValidationFailed) as err:
        to_utc("2026-10-06T10:30", "Mars/Olympus")
    assert err.value.code == "invalid_timezone"


def test_invalid_local_time() -> None:
    with pytest.raises(ValidationFailed) as err:
        to_utc("06/10/2026 10:30", "UTC")
    assert err.value.code == "invalid_local_time"


def test_render_for() -> None:
    at = datetime(2026, 10, 6, 5, 0, tzinfo=UTC)
    assert render_for(at, "Asia/Kolkata") == "Tue 6 Oct 2026, 10:30 IST (UTC+05:30)"
    assert render_for(at, "Europe/London") == "Tue 6 Oct 2026, 06:00 BST (UTC+01:00)"
    assert render_for(at, "America/New_York") == "Tue 6 Oct 2026, 01:00 EDT (UTC-04:00)"
    assert render_for(at, "UTC") == "Tue 6 Oct 2026, 05:00 UTC (UTC+00:00)"


def test_local_iso_round_trip() -> None:
    at = to_utc("2026-10-06T10:30", "Asia/Kolkata")
    assert local_iso(at, "Asia/Kolkata") == "2026-10-06T10:30"
