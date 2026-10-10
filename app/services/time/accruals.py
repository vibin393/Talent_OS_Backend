"""Pure leave maths (Epic 26, FR-135; AD-20 Decimal only, never float).

* ``accrual_for_month`` -- full ``accrual_amount`` for a fully eligible month; a partial month is prorated
  ``amount x eligible_days / days_in_month`` and quantized to **0.25 day** half-up (1.75 x 16/31 -> 1.00;
  1.75 x 10/30 -> 0.50); then capped: ``min(accrual, max(cap - balance, 0))``.
* ``annual_grant_amount`` -- the full amount, prorated by remaining months (0.25 quantum) when joining
  mid-year.
* ``carry_forward`` -- ``carry = min(balance, limit)`` (``None`` = all, 0 = none; a balance <= 0 carries in
  full), ``lapse = balance - carry``.
* Leave years: ``calendar`` (``"2026"``) or ``fiscal`` April-March (``"26-27"`` via ``core.fiscal``).
"""

import dataclasses
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.core.fiscal import fiscal_year, fiscal_year_bounds
from app.core.money import MONEY_CONTEXT

QUARTER = Decimal("0.25")
DAYS_QUANTUM = Decimal("0.01")
ZERO_DAYS = Decimal("0.00")


@dataclasses.dataclass(frozen=True, slots=True)
class AccrualRule:
    accrual_amount: Decimal
    cap: Decimal | None = None


def _check(*values: object) -> None:
    for v in values:
        if isinstance(v, float):
            msg = "float in leave maths"
            raise TypeError(msg)


def q_days(value: Decimal) -> Decimal:
    return value.quantize(DAYS_QUANTUM, rounding=ROUND_HALF_UP, context=MONEY_CONTEXT)


def quantize_quarter(value: Decimal) -> Decimal:
    """Round to the nearest 0.25 day, half-up."""
    _check(value)
    units = MONEY_CONTEXT.divide(value, QUARTER).quantize(
        Decimal(1), rounding=ROUND_HALF_UP, context=MONEY_CONTEXT
    )
    return q_days(MONEY_CONTEXT.multiply(units, QUARTER))


def apply_cap(amount: Decimal, cap: Decimal | None, balance: Decimal) -> Decimal:
    _check(amount, cap, balance)
    if cap is None:
        return q_days(amount)
    room = max(cap - balance, ZERO_DAYS)
    return q_days(min(amount, room))


def accrual_for_month(rule: AccrualRule, balance: Decimal, eligible_days: int, days_in_month: int) -> Decimal:
    _check(rule.accrual_amount, rule.cap, balance)
    if eligible_days <= 0 or days_in_month <= 0 or rule.accrual_amount <= 0:
        return ZERO_DAYS
    if eligible_days >= days_in_month:
        amount = rule.accrual_amount
    else:
        raw = MONEY_CONTEXT.divide(
            MONEY_CONTEXT.multiply(rule.accrual_amount, Decimal(eligible_days)), Decimal(days_in_month)
        )
        # quarter-day rounding of a partial month must never exceed the full monthly amount
        amount = min(quantize_quarter(raw), rule.accrual_amount)
    return apply_cap(amount, rule.cap, balance)


def annual_grant_amount(
    rule: AccrualRule, balance: Decimal, eligible_months: int, months_in_year: int = 12
) -> Decimal:
    _check(rule.accrual_amount, rule.cap, balance)
    if eligible_months <= 0 or rule.accrual_amount <= 0:
        return ZERO_DAYS
    if eligible_months >= months_in_year:
        amount = rule.accrual_amount
    else:
        raw = MONEY_CONTEXT.divide(
            MONEY_CONTEXT.multiply(rule.accrual_amount, Decimal(eligible_months)), Decimal(months_in_year)
        )
        amount = min(quantize_quarter(raw), rule.accrual_amount)
    return apply_cap(amount, rule.cap, balance)


def carry_forward(balance: Decimal, limit: Decimal | None) -> tuple[Decimal, Decimal]:
    """Return ``(carry, lapse)`` with ``carry + lapse == balance``."""
    _check(balance, limit)
    if balance <= 0 or limit is None:
        return q_days(balance), ZERO_DAYS
    carry = q_days(min(balance, limit))
    return carry, q_days(balance - carry)


# ---------------------------------------------------------------- leave years
def leave_year_of(d: date, mode: str) -> str:
    return fiscal_year(d) if mode == "fiscal" else str(d.year)


def leave_year_bounds(key: str, mode: str) -> tuple[date, date]:
    if mode == "fiscal":
        return fiscal_year_bounds(key)
    year = int(key)
    return date(year, 1, 1), date(year, 12, 31)


def next_leave_year(key: str, mode: str) -> str:
    _, end = leave_year_bounds(key, mode)
    return leave_year_of(date(end.year + 1, 1, 1) if mode != "fiscal" else date(end.year, 4, 1), mode)


def months_remaining(first_eligible: date, mode: str) -> int:
    """Months of the leave year from ``first_eligible``'s month to the year end, inclusive."""
    _, end = leave_year_bounds(leave_year_of(first_eligible, mode), mode)
    return (end.year - first_eligible.year) * 12 + end.month - first_eligible.month + 1


def parse_days(raw: object, *, signed: bool = False) -> Decimal | None:
    r"""Strict day parsing for API input: ``-?\\d{1,3}(.\\d{1,2})?``; floats are refused (``None``)."""
    import re

    if raw is None or isinstance(raw, (float, bool)):
        return None
    text = str(raw).strip()
    pattern = r"^-?\d{1,3}(\.\d{1,2})?$" if signed else r"^\d{1,3}(\.\d{1,2})?$"
    if not re.match(pattern, text):
        return None
    return q_days(Decimal(text))
