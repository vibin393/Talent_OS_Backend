"""Pure expense arithmetic and the transition table (Epic 27 A3/A8): Decimal edge cases, per-line rounding,
limits, re-bill, currencies, AI amount parsing; §5.11 transitions incl. human-only and system-only rows."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.actors import ActorRef
from app.core.errors import DomainError, ValidationFailed
from app.models.expenses import ExpenseClaim
from app.services.expenses import amounts
from app.services.expenses.claims import EXPENSE_TRANSITIONS, transition_claim

D = Decimal


def test_fx_conversion_rounds_half_up_once() -> None:
    assert amounts.convert(D("12.35"), D("83.123456"), "INR") == D("1026.57")
    assert amounts.convert(D("0.01"), D("83.5"), "INR") == D("0.84")
    # JPY 1 x 0.505 = 0.505 INR -> 0.51 (HALF_UP; HALF_EVEN would give 0.50)
    assert amounts.convert(D("1"), D("0.505"), "INR") == D("0.51")
    # INR -> JPY (0 minor units)
    assert amounts.convert(D("1000.00"), D("1.785"), "JPY") == D("1785")
    assert amounts.convert(D("10.00"), None, "INR") == D("10.00")


def test_per_line_rounding_not_rounding_the_sum() -> None:
    lines = [amounts.convert(D("0.01"), D("33.5"), "INR") for _ in range(3)]
    assert lines == [D("0.34")] * 3
    assert amounts.total(lines) == D("1.02")
    assert amounts.convert(D("0.03"), D("33.5"), "INR") == D("1.01")  # rounding the sum would differ


def test_mileage_and_markup() -> None:
    assert amounts.mileage_amount(D("37.5"), D("9.5000"), "INR") == D("356.25")
    assert amounts.bill_amount(D("1000.00"), "markup", D("12.5"), "INR") == D("1125.00")
    assert amounts.bill_amount(D("333.33"), "markup", D("10"), "INR") == D("366.66")
    assert amounts.bill_amount(D("1026.57"), "at_cost", None, "INR") == D("1026.57")
    assert amounts.bill_amount(D("5.00"), "not_billable", None, "INR") is None


def test_limits_and_receipt_threshold() -> None:
    assert not amounts.over_limit(D("1500.00") + D("500.00"), D("2000.00"))
    assert amounts.over_limit(D("1500.00") + D("500.00") + D("0.01"), D("2000.00"))
    assert not amounts.over_limit(D("99999"), None)
    assert amounts.receipt_required(D("0.01"), D("0"))
    assert not amounts.receipt_required(D("500.00"), D("500.00"))
    assert not amounts.receipt_required(D("500.00"), None)


def test_currencies_and_parsing() -> None:
    assert amounts.check_currency("inr") == "INR"
    assert amounts.check_currency("JPY") == "JPY"
    for bad in ("KWD", "XXZ", ""):
        with pytest.raises(ValidationFailed) as exc:
            amounts.check_currency(bad)
        assert exc.value.code == "currency_unsupported"
    assert amounts.parse_amount("12.35", "USD") == D("12.35")
    with pytest.raises(ValidationFailed) as exc:
        amounts.parse_amount("-1.00", "INR")
    assert exc.value.code == "negative_amount"
    with pytest.raises(ValidationFailed) as exc:
        amounts.parse_amount("1.5", "JPY")
    assert exc.value.code == "amount_invalid"
    with pytest.raises(ValidationFailed):
        amounts.parse_amount(1.5, "INR")  # type: ignore[arg-type]


def test_ai_amount_parsing_never_uses_float() -> None:
    assert amounts.parse_suggested_amount("1234.50") == D("1234.50")
    assert amounts.parse_suggested_amount("1,234.50") is None
    assert amounts.parse_suggested_amount("₹100") is None
    assert amounts.parse_suggested_amount(1234.5) is None
    assert amounts.parse_suggested_amount("-5") is None


@given(
    amount=st.decimals(
        min_value=D("0"), max_value=D("1000000"), places=2, allow_nan=False, allow_infinity=False
    ),
    fx=st.decimals(
        min_value=D("0.000001"), max_value=D("1000"), places=6, allow_nan=False, allow_infinity=False
    ),
    pct=st.decimals(min_value=D("0"), max_value=D("100"), places=4, allow_nan=False, allow_infinity=False),
)
def test_properties(amount: Decimal, fx: Decimal, pct: Decimal) -> None:
    converted = amounts.convert(amount, fx, "INR")
    assert converted == (amount * fx).quantize(D("0.01"), rounding="ROUND_HALF_UP")
    assert converted >= 0
    assert amounts.bill_amount(converted, "markup", pct, "INR") >= converted  # type: ignore[operator]


HUMAN = ActorRef("user", None)
SYSTEM = ActorRef("system", None)


def _claim(status: str) -> ExpenseClaim:
    return ExpenseClaim(status=status, lock_version=1)


@pytest.mark.parametrize(("frm", "to"), sorted(EXPENSE_TRANSITIONS))
def test_transition_table(frm: str, to: str) -> None:
    definition = EXPENSE_TRANSITIONS[frm, to]
    if definition.system_only:
        with pytest.raises(DomainError):
            transition_claim(_claim(frm), to, HUMAN)
        claim = _claim(frm)
        transition_claim(claim, to, SYSTEM)
        assert claim.status == to
        return
    for kind in ("workflow", "ai", "system"):
        claim = _claim(frm)
        if definition.human_only:
            with pytest.raises(DomainError):
                transition_claim(claim, to, ActorRef(kind, None))  # type: ignore[arg-type]
    claim = _claim(frm)
    transition_claim(claim, to, HUMAN)
    assert claim.status == to and claim.lock_version == 2


def test_star_rows_and_unknown_transitions() -> None:
    star = {k for k, v in EXPENSE_TRANSITIONS.items() if v.human_only}
    for frm in ("submitted", "in_approval"):
        for to in ("approved", "partially_approved", "rejected"):
            assert (frm, to) in star
    with pytest.raises(DomainError):
        transition_claim(_claim("draft"), "approved", HUMAN)
    with pytest.raises(DomainError):
        transition_claim(_claim("reimbursed"), "draft", HUMAN)
