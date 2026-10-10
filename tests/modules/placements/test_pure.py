"""Pure rules of Epic 19: margin (hypothesis, Decimal only), dropout rules (table-driven), the status matrix
(★ Joined is human-only) and the no-AI import guard."""

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.errors import ConflictError
from app.services.placements import dropout, margin, status

RATES = st.decimals(
    min_value=Decimal("0.01"), max_value=Decimal("99999"), places=2, allow_nan=False, allow_infinity=False
)
PCTS = st.decimals(
    min_value=Decimal("0"), max_value=Decimal("60"), places=2, allow_nan=False, allow_infinity=False
)


@given(bill=RATES, pay=RATES, burden=PCTS)
def test_margin_is_bill_minus_burdened_pay(bill: Decimal, pay: Decimal, burden: PCTS) -> None:  # type: ignore[valid-type]
    amount, pct = margin.compute(bill_rate=bill, pay_rate=pay, burden_pct=burden)
    assert isinstance(amount, Decimal) and isinstance(pct, Decimal)
    expected = bill - pay * (1 + burden / 100)  # type: ignore[operator]
    assert abs(amount - expected) <= Decimal("0.005")
    assert amount.as_tuple().exponent == -2
    if amount > 0:
        assert pct > 0
    assert abs(pct - expected / bill * 100) <= Decimal("0.00005")


@given(bill=RATES, vendor=RATES, pay=RATES)
def test_vendor_rate_replaces_pay_as_cost(bill: Decimal, vendor: Decimal, pay: Decimal) -> None:
    amount, _ = margin.compute(
        bill_rate=bill, pay_rate=pay, vendor_rate=vendor, vendor_burden_pct=Decimal("0")
    )
    assert amount == (bill - vendor).quantize(Decimal("0.01"))


def test_margin_half_up_and_missing_inputs() -> None:
    assert margin.compute(bill_rate=Decimal("10.005"), pay_rate=Decimal("0")) == (
        Decimal("10.01"),
        Decimal("100.0000"),
    )
    assert margin.compute(bill_rate=None, pay_rate=Decimal("1")) == (None, None)
    assert margin.compute(bill_rate=Decimal("1"), pay_rate=None) == (None, None)
    with pytest.raises(TypeError):
        margin.compute(bill_rate=1.5, pay_rate=Decimal("1"))  # type: ignore[arg-type]


RULES = {"no_response_touchpoints": 2, "onboarding_pct_at_t7": 50, "counter_offer": True}
T = dropout.TouchpointFact
NOW = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("touchpoints", "pct", "days", "counter", "expected"),
    [
        ([], None, 20, None, []),
        ([T(True, False), T(True, False)], None, 20, None, ["no_response"]),
        ([T(True, False), T(True, True)], None, 20, None, []),
        ([], 40, 7, None, ["low_onboarding"]),
        ([], 40, 8, None, []),
        ([], 60, 3, None, []),
        ([], None, 10, NOW, ["counter_offer"]),
        ([T(True, False)] * 3, 10, 1, NOW, ["no_response", "low_onboarding", "counter_offer"]),
    ],
)
def test_dropout_rules(
    touchpoints: list[dropout.TouchpointFact],
    pct: int | None,
    days: int,
    counter: datetime | None,
    expected: list[str],
) -> None:
    assert dropout.evaluate(RULES, touchpoints, pct, days, counter) == expected


def test_dropout_reraises_only_on_new_rule() -> None:
    assert dropout.newly_fired(["no_response"], ["no_response"]) == []
    assert dropout.newly_fired(["no_response"], ["no_response", "counter_offer"]) == ["counter_offer"]


@pytest.mark.parametrize("actor", ["ai", "workflow", "system", "candidate", "secure_link"])
@pytest.mark.parametrize("source", ["pre_joining", "joining_confirmed"])
def test_joined_is_human_only(actor: str, source: str) -> None:
    with pytest.raises(ConflictError) as exc:
        status.check_transition(source, "active", actor)
    assert exc.value.code == "transition_not_allowed"
    status.check_transition(source, "active", "user")


@pytest.mark.parametrize(
    ("src", "dst", "actor", "ok"),
    [
        ("pre_joining", "joining_confirmed", "candidate", True),
        ("pre_joining", "joining_confirmed", "secure_link", True),
        ("pre_joining", "joining_confirmed", "workflow", False),
        ("pre_joining", "dropped", "user", True),
        ("joining_confirmed", "dropped", "system", False),
        ("active", "dropped", "user", False),
        ("active", "offboarding", "system", True),
        ("active", "offboarding", "user", False),
        ("dropped", "pre_joining", "user", False),
        ("offboarding", "ended", "system", True),
    ],
)
def test_transition_matrix(src: str, dst: str, actor: str, ok: bool) -> None:
    if ok:
        status.check_transition(src, dst, actor)
    else:
        with pytest.raises(ConflictError):
            status.check_transition(src, dst, actor)


def test_placements_never_import_ai_or_upper_domains() -> None:
    root = Path(__file__).resolve().parents[3] / "app" / "services" / "placements"
    forbidden = (
        "app.services.ai",
        "app.services.bgv",
        "app.services.onboarding",
        "app.services.time",
        "app.services.billing",
    )
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                assert not name.startswith(forbidden), f"{path.name} imports {name}"
