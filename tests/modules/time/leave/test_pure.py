"""Pure leave maths (Epic 26): accrual quantization, caps, carry-forward, leave years, day counts,
transitions."""

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.actors import ActorRef
from app.core.errors import ConflictError
from app.models.time import LeaveRequest
from app.services.time import accruals, calendar
from app.services.time.accruals import AccrualRule
from app.services.time.leave_requests import LEAVE_TRANSITIONS, transition_leave

D = Decimal
EL = AccrualRule(D("1.75"))


def test_accrual_partial_months_quantize_to_quarter_days() -> None:
    assert accruals.accrual_for_month(EL, D("0"), 16, 31) == D("1.00")  # 0.903 -> 1.00
    assert accruals.accrual_for_month(EL, D("0"), 10, 30) == D("0.50")  # 0.583 -> 0.50
    assert accruals.accrual_for_month(EL, D("0"), 31, 31) == D("1.75")
    assert accruals.accrual_for_month(EL, D("0"), 0, 31) == D("0.00")


def test_twelve_full_months_is_exact() -> None:
    total = sum((accruals.accrual_for_month(EL, D("0"), 30, 30) for _ in range(12)), D("0"))
    assert total == D("21.00")
    assert all(isinstance(x, Decimal) for x in (total,))


def test_cap() -> None:
    capped = AccrualRule(D("1.75"), D("24.00"))
    assert accruals.accrual_for_month(capped, D("23.50"), 31, 31) == D("0.50")
    assert accruals.accrual_for_month(capped, D("24.00"), 31, 31) == D("0.00")
    assert accruals.accrual_for_month(capped, D("30.00"), 31, 31) == D("0.00")


def test_annual_grant_prorated_by_months() -> None:
    rule = AccrualRule(D("12.00"))
    assert accruals.annual_grant_amount(rule, D("0"), 12) == D("12.00")
    assert accruals.annual_grant_amount(rule, D("0"), 5) == D("5.00")
    assert accruals.annual_grant_amount(AccrualRule(D("10.00")), D("0"), 7) == D("5.75")  # 5.833 -> 5.75


def test_carry_forward() -> None:
    assert accruals.carry_forward(D("13.25"), D("10.00")) == (D("10.00"), D("3.25"))
    assert accruals.carry_forward(D("-1.50"), D("10.00")) == (D("-1.50"), D("0.00"))
    assert accruals.carry_forward(D("4.00"), None) == (D("4.00"), D("0.00"))
    assert accruals.carry_forward(D("4.00"), D("0")) == (D("0.00"), D("4.00"))


def test_floats_are_refused() -> None:
    with pytest.raises(TypeError):
        accruals.accrual_for_month(AccrualRule(1.75), D("0"), 31, 31)  # type: ignore[arg-type]
    assert accruals.parse_days(1.5) is None
    assert accruals.parse_days("-1.5", signed=True) == D("-1.50")
    assert accruals.parse_days("1.555") is None
    assert accruals.parse_days("-1") is None


def test_leave_years() -> None:
    assert accruals.leave_year_of(date(2026, 3, 31), "calendar") == "2026"
    assert accruals.leave_year_of(date(2026, 3, 31), "fiscal") == "25-26"
    assert accruals.leave_year_of(date(2026, 4, 1), "fiscal") == "26-27"
    assert accruals.next_leave_year("2026", "calendar") == "2027"
    assert accruals.next_leave_year("26-27", "fiscal") == "27-28"
    assert accruals.months_remaining(date(2026, 10, 15), "calendar") == 3
    assert accruals.months_remaining(date(2026, 10, 15), "fiscal") == 6


def test_leave_days_half_days_weekend_and_holidays() -> None:
    fri, mon = date(2026, 10, 9), date(2026, 10, 12)
    assert calendar.leave_days(fri, mon, start_half="pm", end_half="am") == D("1.00")
    assert calendar.leave_days(fri, mon) == D("2.00")
    # a holiday inside the range is excluded
    assert calendar.leave_days(date(2026, 10, 12), date(2026, 10, 16), holidays=[date(2026, 10, 14)]) == D(
        "4.00"
    )
    # weekend-only / holiday-only -> zero
    assert calendar.leave_days(date(2026, 10, 10), date(2026, 10, 11)) == D("0.00")
    # half day on a non-working day is not deducted
    assert calendar.leave_days(date(2026, 10, 10), mon, start_half="pm") == D("1.00")
    assert calendar.leave_days(fri, fri, start_half="pm") == D("0.50")
    with pytest.raises(ValueError, match="both_halves"):
        calendar.leave_days(fri, fri, start_half="pm", end_half="am")


@settings(max_examples=60, deadline=None)
@given(
    amount=st.decimals(min_value=D("0.25"), max_value=D("5.00"), places=2),
    cap=st.decimals(min_value=D("0.00"), max_value=D("30.00"), places=2),
    balance=st.decimals(min_value=D("-5.00"), max_value=D("40.00"), places=2),
    eligible=st.integers(min_value=0, max_value=31),
)
def test_accrual_never_exceeds_cap(amount: Decimal, cap: Decimal, balance: Decimal, eligible: int) -> None:
    got = accruals.accrual_for_month(AccrualRule(amount, cap), balance, eligible, 31)
    assert got >= 0
    assert got <= amount
    if got > 0:
        assert balance + got <= max(cap, balance)
    assert got == got.quantize(D("0.01"))


def _req(status: str) -> LeaveRequest:
    return LeaveRequest(status=status)


@pytest.mark.parametrize(("pair", "definition"), list(LEAVE_TRANSITIONS.items()))
@pytest.mark.parametrize("kind", ["workflow", "ai", "system"])
def test_starred_transitions_refuse_automated_actors(
    pair: tuple[str, str], definition: object, kind: str
) -> None:
    req = _req(pair[0])
    if definition.human_only:
        with pytest.raises(ConflictError) as info:
            transition_leave(req, pair[1], ActorRef(kind, None))  # type: ignore[arg-type]
        assert info.value.code == "transition_not_allowed"
        assert req.status == pair[0]
    else:
        transition_leave(req, pair[1], ActorRef(kind, None))  # type: ignore[arg-type]
        assert req.status == pair[1]


def test_transition_matrix_is_exactly_5_10() -> None:
    assert set(LEAVE_TRANSITIONS) == {
        ("requested", "approved"),
        ("requested", "rejected"),
        ("requested", "withdrawn"),
        ("approved", "cancellation_requested"),
        ("cancellation_requested", "cancelled"),
        ("cancellation_requested", "approved"),
    }
    req = _req("withdrawn")
    with pytest.raises(ConflictError):
        transition_leave(req, "approved", ActorRef("user", None))
    for to in ("approved", "rejected"):
        r = _req("requested")
        transition_leave(r, to, ActorRef("user", None))
        assert r.status == to
