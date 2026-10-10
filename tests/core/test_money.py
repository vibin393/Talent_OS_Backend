"""AD-20: money arithmetic is Decimal-only, ROUND_HALF_UP, currency-aware (Epic 13, story 13.1)."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from app.core.errors import ValidationFailed
from app.core.money import (
    DecimalStr,
    Money,
    MoneyOut,
    assert_document_currency,
    line_taxable,
    minor_units,
    percent_of,
    quantize_amount,
    quantize_fx,
    quantize_hours,
    quantize_rate,
    to_decimal,
)

amounts = st.decimals(min_value=Decimal("-1e12"), max_value=Decimal("1e12"), allow_nan=False, allow_infinity=False, places=6)


@given(amounts)
def test_quantize_is_idempotent(value: Decimal) -> None:
    once = quantize_amount(value, "INR")
    assert quantize_amount(once, "INR") == once


@given(amounts)
def test_sign_symmetry(value: Decimal) -> None:
    assert quantize_amount(-value, "USD") == -quantize_amount(value, "USD")


@given(st.integers(min_value=-10**9, max_value=10**9))
def test_half_up_at_half(cents: int) -> None:
    value = Decimal(cents) / 100 + Decimal("0.005") * (1 if cents >= 0 else -1)
    expected = (Decimal(cents) + (1 if cents >= 0 else -1)) / 100
    assert quantize_amount(value, "INR") == expected


def test_half_up_examples() -> None:
    assert quantize_amount(Decimal("2.345"), "INR") == Decimal("2.35")
    assert quantize_amount(Decimal("-2.345"), "INR") == Decimal("-2.35")
    assert quantize_amount(Decimal("2.5"), "JPY") == Decimal("3")
    assert quantize_rate(Decimal("1.00005")) == Decimal("1.0001")
    assert quantize_hours(Decimal("7.995")) == Decimal("8.00")
    assert quantize_fx(Decimal("83.1234565")) == Decimal("83.123457")


def test_float_refused_everywhere() -> None:
    with pytest.raises(TypeError):
        to_decimal(0.1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        quantize_amount(0.1, "INR")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(0.5, "INR")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(Decimal("1"), "INR") + 0.5  # type: ignore[operator]


def test_to_decimal_accepts_strings_and_refuses_nan() -> None:
    assert to_decimal("1e3") == Decimal("1000")
    assert to_decimal(7) == Decimal(7)
    for bad in ("NaN", "Infinity", "-inf"):
        with pytest.raises(ValueError):
            to_decimal(bad)
    with pytest.raises(ValueError):
        to_decimal("abc")


def test_mixed_currency_refused() -> None:
    with pytest.raises(ValueError, match="mixed currencies"):
        Money(Decimal("1"), "INR") + Money(Decimal("1"), "USD")
    with pytest.raises(ValueError, match="mixed currencies"):
        Money(Decimal("1"), "INR") - Money(Decimal("1"), "USD")


def test_money_arithmetic_and_api() -> None:
    total = Money(Decimal("10.005"), "INR") + Money(Decimal("0.01"), "INR")
    assert total.amount == Decimal("10.015")
    assert (-total).amount == Decimal("-10.015")
    assert total.quantized().amount == Decimal("10.02")
    assert total.to_api() == {"amount": "10.02", "currency": "INR"}


def test_document_currency_rules() -> None:
    assert minor_units("KWD") == 3
    assert minor_units("JPY") == 0
    assert_document_currency("INR")
    assert_document_currency("JPY")
    with pytest.raises(ValidationFailed) as exc:
        assert_document_currency("KWD")
    assert exc.value.code == "currency_not_supported"
    with pytest.raises(ValidationFailed):
        assert_document_currency("XXX")


def test_line_taxable_rounds_once() -> None:
    # 3 x 33.335 x 1.5 = 150.0075 -> 150.01 (rounding per factor would give a different result)
    assert line_taxable(Decimal("3"), Decimal("33.335"), Decimal("1.5"), "INR") == Decimal("150.01")
    assert percent_of(Decimal("100.05"), Decimal("18"), "INR") == Decimal("18.01")


def test_decimal_serialises_as_string() -> None:
    class Out(BaseModel):
        value: DecimalStr

    assert Out(value=Decimal("1.10")).model_dump(mode="json") == {"value": "1.10"}
    assert MoneyOut(amount=Decimal("5.00"), currency="INR").model_dump(mode="json") == {
        "amount": "5.00",
        "currency": "INR",
    }
