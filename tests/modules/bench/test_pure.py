"""Bench arithmetic (Decimal only, quantized once) and the §5.14 transition table."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.errors import ConflictError
from app.services.placements import bench
from app.services.placements.bench_cost import (
    CostSettings,
    bench_days,
    cost_by_month,
    cost_to_date,
    daily_cost,
)

S = CostSettings(hours_per_day=Decimal("8.00"), monthly_rate_divisor=Decimal("22.00"))


@pytest.mark.parametrize(
    ("rate", "unit", "currency", "expected"),
    [
        ("437.50", "hour", "INR", "3500.00"),
        ("100000.00", "month", "INR", "4545.45"),
        ("55555.55", "month", "INR", "2525.25"),
        ("0.125", "day", "INR", "0.13"),
        ("0.125", "day", "JPY", "0"),
        ("2500", "day", "INR", "2500.00"),
    ],
)
def test_daily_cost(rate: str, unit: str, currency: str, expected: str) -> None:
    value = daily_cost(Decimal(rate), unit, currency, S)
    assert isinstance(value, Decimal)
    assert value == Decimal(expected) and str(value) == expected


def test_daily_cost_unknown_unit_and_floats() -> None:
    assert daily_cost(Decimal("10"), "piece", "INR", S) is None
    assert daily_cost(Decimal("10"), "fixed", "INR", S) is None
    assert daily_cost(None, "hour", "INR", S) is None
    with pytest.raises(TypeError):
        daily_cost(437.5, "hour", "INR", S)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        daily_cost(Decimal("1"), "hour", "INR", CostSettings(8.0, Decimal(22)))  # type: ignore[arg-type]


def test_bench_days() -> None:
    fri, mon = date(2026, 10, 2), date(2026, 10, 5)
    assert bench_days(fri, mon, "weekdays") == 2
    assert bench_days(fri, mon, "calendar") == 4
    assert bench_days(date(2026, 10, 31), date(2026, 11, 1), "calendar") == 2  # month end
    assert bench_days(date(2028, 2, 28), date(2028, 3, 1), "calendar") == 3  # leap day 2028-02-29
    assert bench_days(date(2028, 2, 28), date(2028, 3, 1), "weekdays") == 3  # Mon, Tue (29th), Wed
    assert bench_days(date(2030, 1, 1), date(2026, 1, 1), "weekdays") == 0  # future available_from
    assert bench_days(fri, fri, "weekdays") == 1


def test_cost_by_month_three_months_each_product_quantized() -> None:
    rate = Decimal("4545.45")
    start, until = date(2026, 1, 20), date(2026, 3, 10)
    months = cost_by_month(rate, "INR", start, until, "calendar")
    assert list(months) == ["2026-01", "2026-02", "2026-03"]
    assert months["2026-01"] == Decimal("54545.40")  # 12 days
    assert months["2026-02"] == Decimal("127272.60")  # 28 days
    assert months["2026-03"] == Decimal("45454.50")  # 10 days
    total = cost_to_date(rate, bench_days(start, until, "calendar"), "INR")
    assert total is not None
    # each entry-month product is quantized: the sum differs from one product by at most 0.005 per month
    assert abs(sum(months.values()) - total) <= Decimal("0.015")
    odd = cost_by_month(Decimal("0.333"), "INR", date(2026, 1, 31), date(2026, 3, 1), "calendar")
    single = cost_to_date(Decimal("0.333"), 30, "INR")
    assert single is not None and abs(sum(odd.values()) - single) <= Decimal("0.015")


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31)),
    st.integers(min_value=0, max_value=400),
    st.decimals(
        min_value=Decimal(0), max_value=Decimal("100000"), places=2, allow_nan=False, allow_infinity=False
    ),
)
def test_properties(start: date, span: int, rate: Decimal) -> None:
    until = start + timedelta(days=span)
    assert bench_days(start, until, "weekdays") <= bench_days(start, until, "calendar")
    cost = cost_to_date(rate, bench_days(start, until, "weekdays"), "INR")
    assert cost is not None and cost >= 0
    daily = daily_cost(rate, "month", "INR", S)
    assert daily is not None and daily >= 0


TRANSITION_MATRIX = [
    ("on_bench", "proposed", "system", True),
    ("on_bench", "proposed", "user", False),
    ("proposed", "on_bench", "system", True),
    ("proposed", "on_bench", "user", False),
    ("on_bench", "redeployed", "system", True),
    ("proposed", "redeployed", "system", True),
    ("on_bench", "redeployed", "user", False),
    ("on_bench", "released", "user", True),
    ("proposed", "released", "user", True),
    ("on_bench", "released", "workflow", False),
    ("on_bench", "released", "ai", False),
    ("on_bench", "released", "system", False),
    ("on_bench", "released", "candidate", False),
    ("on_bench", "on_bench", "system", False),
]


@pytest.mark.parametrize(("frm", "to", "actor", "ok"), TRANSITION_MATRIX)
def test_transition_matrix(frm: str, to: str, actor: str, ok: bool) -> None:
    if ok:
        bench.check_transition(frm, to, actor)
    else:
        with pytest.raises(ConflictError) as exc:
            bench.check_transition(frm, to, actor)
        assert exc.value.code == "transition_not_allowed"


@pytest.mark.parametrize("terminal", ["redeployed", "released"])
@pytest.mark.parametrize("to", ["on_bench", "proposed", "redeployed", "released"])
@pytest.mark.parametrize("actor", ["user", "system", "candidate", "workflow"])
def test_terminal_states_refuse_everything(terminal: str, to: str, actor: str) -> None:
    with pytest.raises(ConflictError):
        bench.check_transition(terminal, to, actor, "contractor_opt_in")


def test_candidate_may_release_only_contractor_opt_in() -> None:
    bench.check_transition("on_bench", "released", "candidate", "contractor_opt_in")
    with pytest.raises(ConflictError):
        bench.check_transition("on_bench", "released", "candidate", "placement_ended")
