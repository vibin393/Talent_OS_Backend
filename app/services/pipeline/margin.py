"""Margin (FR-37). Pure: no I/O.

``margin_amount = bill - pay * (1 + burden% / 100)`` and ``margin_pct = margin_amount / bill * 100``, both
rounded half-up to 2 dp, after normalising pay and bill to the demand's rate unit with ``HOURS_PER``
(hour=1, day=8, month=168, year=2016 hours). Pay and bill must share one currency (and match the demand's
currency when it has one) -- otherwise 422 ``currency_mismatch``.
"""

import dataclasses
from decimal import ROUND_HALF_UP, Decimal

from app.core.errors import ValidationFailed

HOURS_PER: dict[str, Decimal] = {
    "hour": Decimal(1),
    "day": Decimal(8),
    "month": Decimal(168),
    "year": Decimal(2016),
}
CENT = Decimal("0.01")


@dataclasses.dataclass(frozen=True, slots=True)
class Margin:
    pay: Decimal  # normalised to ``rate_unit``
    bill: Decimal  # normalised to ``rate_unit``
    currency: str
    rate_unit: str
    burden_pct: Decimal
    amount: Decimal
    pct: Decimal
    min_pct: Decimal
    below_min: bool


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def normalize(amount: Decimal, from_unit: str, to_unit: str) -> Decimal:
    """Convert a rate between units (e.g. 1600/day -> 200/hour)."""
    if from_unit not in HOURS_PER or to_unit not in HOURS_PER:
        raise ValidationFailed(
            "Unknown rate unit", code="invalid_rate_unit", details={"units": [from_unit, to_unit]}
        )
    if from_unit == to_unit:
        return amount
    return amount / HOURS_PER[from_unit] * HOURS_PER[to_unit]


def check_currencies(pay_currency: str, bill_currency: str, demand_currency: str | None) -> str:
    if pay_currency != bill_currency or (demand_currency and demand_currency != pay_currency):
        raise ValidationFailed(
            "Pay rate, bill rate and the demand must use one currency",
            code="currency_mismatch",
            details={"pay": pay_currency, "bill": bill_currency, "demand": demand_currency},
        )
    return pay_currency


def compute(
    *,
    pay: Decimal,
    bill: Decimal,
    pay_currency: str,
    bill_currency: str,
    rate_unit: str,
    burden_pct: Decimal,
    min_pct: Decimal,
    pay_unit: str | None = None,
    bill_unit: str | None = None,
    demand_currency: str | None = None,
) -> Margin:
    currency = check_currencies(pay_currency, bill_currency, demand_currency)
    if bill <= 0:
        raise ValidationFailed("The bill rate must be greater than zero", code="invalid_bill_rate")
    pay_n = normalize(pay, pay_unit or rate_unit, rate_unit)
    bill_n = normalize(bill, bill_unit or rate_unit, rate_unit)
    amount = bill_n - pay_n * (Decimal(1) + burden_pct / Decimal(100))
    pct = amount / bill_n * Decimal(100)
    amount_q, pct_q = q2(amount), q2(pct)
    return Margin(
        pay=q2(pay_n),
        bill=q2(bill_n),
        currency=currency,
        rate_unit=rate_unit,
        burden_pct=burden_pct,
        amount=amount_q,
        pct=pct_q,
        min_pct=min_pct,
        below_min=pct_q < min_pct,
    )
