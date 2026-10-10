# ruff: noqa: E501
"""Pure pricing (AD-20, FR-147): no DB, no I/O, no clock. Day counting lives only in ``time/calendar.py``; this
module multiplies and applies the last-segment remainder rule.

* ``effective_rate = quantize4(rate x multiplier)`` (stored and printed at 4 dp);
* ``line_amount = quantize(qty x effective_rate)`` once, half-up;
* ``prorate_monthly``: a monthly rate split over rate segments by working days; when every segment has the same
  rate and the segments cover the month, the last segment takes the remainder so a full month bills exactly the
  rate;
* taxable = gross - discount, refused when negative (``negative_line``).
"""

from collections.abc import Sequence
from decimal import Decimal

from app.core.money import MONEY_CONTEXT, quantize_amount, quantize_rate, to_decimal


class PricingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def effective_rate(rate: Decimal, multiplier: Decimal = Decimal(1)) -> Decimal:
    return quantize_rate(MONEY_CONTEXT.multiply(to_decimal(rate), to_decimal(multiplier)))


def line_amount(quantity: Decimal, rate: Decimal, multiplier: Decimal, currency: str) -> Decimal:
    return quantize_amount(
        MONEY_CONTEXT.multiply(to_decimal(quantity), effective_rate(rate, multiplier)), currency
    )


def taxable(gross: Decimal, discount: Decimal, currency: str) -> Decimal:
    gross, discount = to_decimal(gross), to_decimal(discount)
    if discount < 0 or gross < 0:
        raise PricingError("negative_line", "Invoice lines cannot be negative")
    value = quantize_amount(MONEY_CONTEXT.subtract(gross, discount), currency)
    if value < 0:
        raise PricingError("negative_line", "The discount is larger than the line amount")
    return value


def markup(amount: Decimal, pct: Decimal | None, currency: str) -> Decimal:
    if not pct:
        return quantize_amount(to_decimal(amount), currency)
    factor = MONEY_CONTEXT.add(Decimal(1), MONEY_CONTEXT.divide(to_decimal(pct), Decimal(100)))
    return quantize_amount(MONEY_CONTEXT.multiply(to_decimal(amount), factor), currency)


def prorate_monthly(
    segments: Sequence[tuple[Decimal, int]], month_working_days: int, currency: str
) -> list[Decimal]:
    """``segments`` = ``[(monthly_rate, billable_working_days), ...]`` in date order; returns one amount per
    segment. A full month at a single rate bills exactly the rate (last segment = remainder)."""
    if month_working_days <= 0:
        return [quantize_amount(Decimal(0), currency) for _ in segments]
    amounts = [
        quantize_amount(
            MONEY_CONTEXT.divide(
                MONEY_CONTEXT.multiply(to_decimal(rate), Decimal(days)), Decimal(month_working_days)
            ),
            currency,
        )
        for rate, days in segments
    ]
    rates = {to_decimal(r) for r, _ in segments}
    if segments and len(rates) == 1 and sum(d for _, d in segments) == month_working_days:
        rate = next(iter(rates))
        amounts[-1] = quantize_amount(rate - sum(amounts[:-1], Decimal(0)), currency)
    return amounts


def prorate_factor(monthly_rate: Decimal, proration_factor: Decimal, currency: str) -> Decimal:
    """Single segment: ``quantize(rate x factor)`` (factor from ``time_service.billing_inputs``; 1 = full month)."""
    if to_decimal(proration_factor) >= 1:
        return quantize_amount(to_decimal(monthly_rate), currency)
    return quantize_amount(
        MONEY_CONTEXT.multiply(to_decimal(monthly_rate), to_decimal(proration_factor)), currency
    )


def round_off(total: Decimal, mode: str, currency: str) -> Decimal:
    """``nearest_rupee`` on INR documents: the half-up adjustment to the nearest whole unit; otherwise 0."""
    if mode != "nearest_rupee" or currency != "INR":
        return Decimal("0.00")
    whole = to_decimal(total).quantize(Decimal(1), context=MONEY_CONTEXT)
    return quantize_amount(whole - to_decimal(total), currency)
