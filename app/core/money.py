"""Money, rate, hour and FX arithmetic (AD-20). ``decimal.Decimal`` only -- never ``float``.

* ``MONEY_CONTEXT``: precision 34, ``ROUND_HALF_UP``; ``InvalidOperation`` and ``FloatOperation`` trap, so a
  float that sneaks into an operation raises instead of rounding silently.
* Amounts are quantized to the currency's ISO-4217 minor units (``core/data/currencies.json``, mirrored by the
  platform table ``currencies``). Document currencies are limited to 0/2 minor units.
* Line rule: ``quantity x rate x multiplier`` is computed unrounded and quantized ONCE (``line_taxable``).
* ``DecimalStr`` / ``MoneyOut``: pydantic types that serialise Decimals as strings in JSON (AD-12).
"""

import dataclasses
import decimal
import functools
import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, PlainSerializer

from app.core.errors import ValidationFailed

MONEY_CONTEXT = decimal.Context(
    prec=34,
    rounding=decimal.ROUND_HALF_UP,
    traps=[decimal.InvalidOperation, decimal.FloatOperation, decimal.DivisionByZero],
)

_DATA = Path(__file__).resolve().parent / "data" / "currencies.json"
_RATE_QUANTUM = Decimal("0.0001")
_HOURS_QUANTUM = Decimal("0.01")
_FX_QUANTUM = Decimal("0.000001")


@functools.cache
def _currencies() -> dict[str, dict[str, Any]]:
    rows = json.loads(_DATA.read_text(encoding="utf-8"))
    return {str(r["code"]): r for r in rows}


def to_decimal(value: Decimal | int | str) -> Decimal:
    """Exact Decimal of ``value``. Floats are refused (``TypeError``); NaN / Infinity are refused
    (``ValueError``); scientific notation strings (``'1e3'``) are accepted."""
    if isinstance(value, (bool, float)):
        msg = f"money values must not be float/bool (got {type(value).__name__})"
        raise TypeError(msg)
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except decimal.InvalidOperation as exc:
            msg = f"not a decimal number: {value!r}"
            raise ValueError(msg) from exc
    else:
        msg = f"unsupported money value type {type(value).__name__}"  # type: ignore[unreachable]
        raise TypeError(msg)
    if not result.is_finite():
        msg = "NaN / Infinity are not valid amounts"
        raise ValueError(msg)
    return result


def minor_units(currency: str) -> int:
    row = _currencies().get(currency.upper())
    if row is None:
        msg = f"unknown currency {currency!r}"
        raise ValueError(msg)
    return int(row["minor_units"])


def assert_document_currency(currency: str) -> None:
    """Document currencies must exist and have 0 or 2 minor units (422 ``currency_not_supported``)."""
    row = _currencies().get((currency or "").upper())
    if row is None or int(row["minor_units"]) not in (0, 2) or not row.get("document_allowed", False):
        raise ValidationFailed(
            "This currency cannot be used on documents",
            code="currency_not_supported",
            details={"currency": currency},
        )


def _quantum(places: int) -> Decimal:
    return Decimal(1).scaleb(-places)


def _q(value: Decimal, quantum: Decimal) -> Decimal:
    return to_decimal(value).quantize(quantum, context=MONEY_CONTEXT)


def quantize_amount(value: Decimal, currency: str) -> Decimal:
    return _q(value, _quantum(minor_units(currency)))


def quantize_rate(value: Decimal) -> Decimal:
    return _q(value, _RATE_QUANTUM)


def quantize_hours(value: Decimal) -> Decimal:
    return _q(value, _HOURS_QUANTUM)


def quantize_fx(value: Decimal) -> Decimal:
    return _q(value, _FX_QUANTUM)


def line_taxable(quantity: Decimal, rate: Decimal, multiplier: Decimal, currency: str) -> Decimal:
    """``quantity x rate x multiplier`` unrounded, then quantized once to the currency's minor units."""
    product = MONEY_CONTEXT.multiply(
        MONEY_CONTEXT.multiply(to_decimal(quantity), to_decimal(rate)), to_decimal(multiplier)
    )
    return quantize_amount(product, currency)


def percent_of(amount: Decimal, pct: Decimal, currency: str) -> Decimal:
    product = MONEY_CONTEXT.multiply(to_decimal(amount), to_decimal(pct))
    return quantize_amount(MONEY_CONTEXT.divide(product, Decimal(100)), currency)


@dataclasses.dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", to_decimal(self.amount))
        object.__setattr__(self, "currency", self.currency.upper())
        minor_units(self.currency)  # unknown currency -> ValueError

    def _same(self, other: object) -> "Money":
        if isinstance(other, float):
            msg = "float operands are not allowed in money arithmetic"
            raise TypeError(msg)
        if not isinstance(other, Money):
            return NotImplemented  # type: ignore[no-any-return]
        if other.currency != self.currency:
            msg = f"mixed currencies: {self.currency} vs {other.currency}"
            raise ValueError(msg)
        return other

    def __add__(self, other: object) -> "Money":
        o = self._same(other)
        if o is NotImplemented:
            return NotImplemented
        return Money(MONEY_CONTEXT.add(self.amount, o.amount), self.currency)

    def __sub__(self, other: object) -> "Money":
        o = self._same(other)
        if o is NotImplemented:
            return NotImplemented
        return Money(MONEY_CONTEXT.subtract(self.amount, o.amount), self.currency)

    def __neg__(self) -> "Money":
        return Money(MONEY_CONTEXT.minus(self.amount), self.currency)

    def quantized(self) -> "Money":
        return Money(quantize_amount(self.amount, self.currency), self.currency)

    def to_api(self) -> dict[str, str]:
        return {"amount": str(self.quantized().amount), "currency": self.currency}


DecimalStr = Annotated[Decimal, PlainSerializer(str, return_type=str, when_used="json")]


class MoneyOut(BaseModel):
    amount: DecimalStr
    currency: str
