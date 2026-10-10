"""Pure time maths (Epic 25 A4, A14): OT/DT split, calendar, start/end hours, unbilled value rounding."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.money import line_taxable
from app.schemas.time import SpanIn
from app.services.time import calendar, overtime
from app.services.time.common import parse_hours
from app.services.time.monitoring import entry_unbilled_value
from app.services.time.timesheets import TIMESHEET_TRANSITIONS, span_hours, transition_not_allowed

D = Decimal
MON = date(2026, 10, 5)


def _days(*hours: str) -> list[overtime.DayInput]:
    return [overtime.DayInput(MON + timedelta(days=i), D(h)) for i, h in enumerate(hours)]


def test_daily_split_ot_and_dt() -> None:
    (s,) = overtime.split(_days("12.50"), overtime.OtRules(D("9"), D("12"), None))
    assert (s.regular, s.ot, s.double_time) == (D("9.00"), D("3.00"), D("0.50"))


def test_acceptance_week_split() -> None:
    out = overtime.split(_days("10.50", "8", "8", "8", "8"), overtime.OtRules(D("9"), None, None))
    assert sum(s.regular for s in out) == D("41.00")
    assert sum(s.ot for s in out) == D("1.50")


def test_weekly_threshold_across_period_boundary() -> None:
    # Week starts Monday; this period starts Thursday; 24.00 regular already counted Mon-Wed (earlier period).
    thu = MON + timedelta(days=3)
    days = [overtime.DayInput(thu + timedelta(days=i), D("8")) for i in range(3)]
    out = overtime.split(days, overtime.OtRules(None, None, D("40")), 0, {MON: D("24.00")})
    assert [s.regular for s in out] == [D("8"), D("8"), D("0")]
    assert [s.ot for s in out] == [D("0"), D("0"), D("8")]


def test_no_thresholds_is_identity() -> None:
    out = overtime.split(_days("7.25", "13"), overtime.OtRules())
    assert [(s.regular, s.ot, s.double_time) for s in out] == [(D("7.25"), 0, 0), (D("13"), 0, 0)]


hours_st = st.integers(min_value=0, max_value=2400).map(lambda n: D(n) / 100)
opt_threshold = st.one_of(st.none(), st.integers(min_value=0, max_value=2400).map(lambda n: D(n) / 100))


@settings(max_examples=150, deadline=None)
@given(st.lists(hours_st, min_size=1, max_size=14), opt_threshold, opt_threshold, opt_threshold)
def test_split_properties(
    worked: list[Decimal], ot: Decimal | None, dt: Decimal | None, weekly: Decimal | None
) -> None:
    if ot is not None and dt is not None and dt < ot:
        ot, dt = dt, ot
    days = [overtime.DayInput(MON + timedelta(days=i), w) for i, w in enumerate(worked)]
    out = overtime.split(days, overtime.OtRules(ot, dt, weekly))
    for s, w in zip(out, worked, strict=True):
        assert s.regular + s.ot + s.double_time == w
        assert s.regular >= 0 and s.ot >= 0 and s.double_time >= 0


@settings(max_examples=100, deadline=None)
@given(hours_st, hours_st)
def test_split_monotonic(a: Decimal, b: Decimal) -> None:
    lo, hi = sorted((a, b))
    rules = overtime.OtRules(D("8"), D("12"), None)
    (x,) = overtime.split([overtime.DayInput(MON, lo)], rules)
    (y,) = overtime.split([overtime.DayInput(MON, hi)], rules)
    assert y.ot + y.double_time >= x.ot + x.double_time


@settings(max_examples=100, deadline=None)
@given(
    st.dates(min_value=date(2024, 1, 1), max_value=date(2028, 12, 31)),
    st.integers(min_value=0, max_value=60),
    st.lists(st.dates(min_value=date(2024, 1, 1), max_value=date(2029, 3, 31)), max_size=10),
)
def test_working_days_properties(start: date, span: int, holidays: list[date]) -> None:
    end = start + timedelta(days=span)
    n = calendar.working_days(start, end, calendar.DEFAULT_WORK_WEEK, holidays)
    assert 0 <= n <= span + 1
    doubled = calendar.working_days(start, end, calendar.DEFAULT_WORK_WEEK, holidays + holidays)
    assert doubled == n  # each holiday excluded once
    assert n == calendar.working_days(start, end) - len(
        {h for h in holidays if start <= h <= end and h.weekday() < 5}
    )


def test_proration_and_periods() -> None:
    # October 2026: 22 working days (Mon-Fri); 5..16 Oct = 10 working days
    assert calendar.month_working_days(date(2026, 10, 1)) == 22
    assert calendar.proration_factor(date(2026, 10, 5), date(2026, 10, 16)) == D("0.454545")
    assert calendar.period_bounds("weekly", 0, date(2026, 10, 8)) == (MON, MON + timedelta(days=6))
    assert calendar.period_bounds("semimonthly", 0, date(2026, 2, 20)) == (
        date(2026, 2, 16),
        date(2026, 2, 28),
    )
    assert calendar.period_bounds("monthly", 0, date(2026, 2, 20)) == (date(2026, 2, 1), date(2026, 2, 28))
    s, e = calendar.period_bounds("biweekly", 0, date(2026, 10, 8))
    assert (e - s).days == 13 and s.weekday() == 0 and s <= date(2026, 10, 8) <= e
    assert len(calendar.periods_between("weekly", 0, date(2026, 10, 1), date(2026, 10, 20))) == 4


def test_start_end_hours_use_day_total_minutes() -> None:
    hours, *_ = span_hours([SpanIn(start="09:00", end="17:20", break_minutes=30)])
    assert hours == D("7.83")
    three, *_ = span_hours([SpanIn(start=f"{h}:00", end=f"{h}:20") for h in ("09", "10", "11")])
    assert three == D("1.00")
    bad, *_ = span_hours([SpanIn(start="10:00", end="09:00")])
    assert bad is None


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ("7.83", True),
        ("24", True),
        ("7.333", False),
        ("0.005", False),
        ("7,5", False),
        ("-1", False),
        ("abc", False),
    ],
)
def test_parse_hours_is_strict(raw: str, ok: bool) -> None:
    assert (parse_hours(raw) is not None) is ok


def test_unbilled_value_rounds_once_per_entry_half_up() -> None:
    intermediate = D("7.83") * D("1234.5678") * D("1.5000")
    assert intermediate == D("14499.99881100")  # unrounded intermediate
    assert entry_unbilled_value(D("7.83"), D("1234.5678"), D("1.5000"), "INR") == D("14500.00")
    assert entry_unbilled_value(D("0.50"), D("100.0100"), D("1.0000"), "INR") == D(
        "50.01"
    )  # HALF_EVEN: 50.00
    assert line_taxable(D("0.50"), D("100.0100"), D("1"), "INR") == D("50.01")
    assert entry_unbilled_value(
        D("4.00"), D("8000"), D("1"), "INR", rate_unit="day", hours_per_day=D("8")
    ) == D("4000.00")
    assert entry_unbilled_value(D("4.00"), D("8000"), D("1"), "INR", rate_unit="month") is None


def test_transition_table() -> None:
    starred = {k for k, v in TIMESHEET_TRANSITIONS.items() if v.human_only}
    assert ("submitted", "in_approval") in starred and ("in_approval", "approved") in starred
    assert ("approved", "draft") in starred
    assert ("draft", "approved") not in TIMESHEET_TRANSITIONS
    assert transition_not_allowed("approved", "submitted").code == "transition_not_allowed"


def test_transition_refuses_automated_actors_on_starred_rows() -> None:
    from app.core.actors import ActorRef
    from app.core.errors import Forbidden
    from app.models.time import Timesheet
    from app.services.time.timesheets import transition_timesheet

    for kind in ("workflow", "ai", "system"):
        ts = Timesheet(status="submitted", lock_version=1)
        with pytest.raises(Forbidden):
            transition_timesheet(ts, "approved", ActorRef(kind, None))  # type: ignore[arg-type]
    ts = Timesheet(status="submitted", lock_version=1)
    transition_timesheet(ts, "approved", ActorRef("system", None), auto=True)  # empty-route auto approval
    assert ts.status == "approved"
