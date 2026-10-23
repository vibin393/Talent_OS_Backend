# ruff: file-ignore[line-too-long]
"""Committed cost arithmetic (Epic 44, FR-225). Pure: no I/O, ``Decimal`` only (AD-14 / AD-20).

* ``months_remaining(start, period_end)``: months from ``start`` to the end of the plan period; the start month is
  pro-rated by the days left in it (``(days_in_month - day + 1) / days_in_month``); 0 when ``start`` is after
  the period. 4 dp.
* ``annualise(rate, unit, hours, days)``: an Offer pay rate to an annual amount (``year`` x1, ``month`` x12,
  ``day`` x days x 12, ``hour`` x hours x 12).
* ``monthly(rate, unit, hours, days)``: a Placement rate segment to a monthly amount (``hour`` x standard hours per
  month, ``day`` x standard days, ``month`` x1, ``year`` / ``annual`` / 12; ``fixed`` -> ``None``: a one-off
  amount counted once by the caller).
* ``permanent_commitment`` = annual CTC x months / 12; ``contract_commitment`` = monthly x months. Results are
  quantised to 2 dp (ROUND_HALF_UP).
"""

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
MONTHS_Q = Decimal("0.0001")


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def months_remaining(start: date, period_end: date) -> Decimal:
    if start > period_end:
        return Decimal(0)
    days_in = calendar.monthrange(start.year, start.month)[1]
    if (start.year, start.month) == (period_end.year, period_end.month):
        first = Decimal(period_end.day - start.day + 1) / Decimal(days_in)
        return first.quantize(MONTHS_Q, rounding=ROUND_HALF_UP)
    first = Decimal(days_in - start.day + 1) / Decimal(days_in)
    whole = (period_end.year - start.year) * 12 + (period_end.month - start.month) - 1
    end_days = calendar.monthrange(period_end.year, period_end.month)[1]
    last = Decimal(period_end.day) / Decimal(end_days)
    return (first + Decimal(whole) + last).quantize(MONTHS_Q, rounding=ROUND_HALF_UP)


def annualise(rate: Decimal, unit: str, hours_per_month: Decimal, days_per_month: Decimal) -> Decimal:
    factor = {
        "year": Decimal(1),
        "annual": Decimal(1),
        "month": Decimal(12),
        "day": days_per_month * 12,
        "hour": hours_per_month * 12,
    }.get(unit)
    if factor is None:
        msg = f"unknown rate unit {unit!r}"
        raise ValueError(msg)
    return rate * factor


def monthly(rate: Decimal, unit: str, hours_per_month: Decimal, days_per_month: Decimal) -> Decimal | None:
    if unit == "fixed":
        return None
    factor = {
        "month": Decimal(1),
        "day": days_per_month,
        "hour": hours_per_month,
        "year": Decimal(1) / Decimal(12),
        "annual": Decimal(1) / Decimal(12),
    }.get(unit)
    if factor is None:
        msg = f"unknown rate unit {unit!r}"
        raise ValueError(msg)
    return rate * factor


def permanent_commitment(annual_ctc: Decimal, months: Decimal) -> Decimal:
    return q2(annual_ctc * months / Decimal(12))


def contract_commitment(monthly_amount: Decimal, months: Decimal) -> Decimal:
    return q2(monthly_amount * months)


def convert(amount: Decimal, rate: Decimal) -> Decimal:
    return q2(amount * rate)
