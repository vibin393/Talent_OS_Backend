"""Pure bench arithmetic (Epic 24, FR-125 / FR-127; AD-20). ``Decimal`` only, never ``float``.

* ``daily_cost``: a pay rate turned into a cost per bench day, quantized ONCE at the end (ROUND_HALF_UP).
* ``bench_days``: inclusive count of bench days (``calendar`` or ``weekdays``); 0 for a future start.
* ``cost_by_month``: per calendar month, ``cost_per_day x bench days in that month``, each product quantized.
"""

import calendar as _calendar
import dataclasses
from collections.abc import Iterable
from datetime import date, timedelta
from decimal import Decimal

from app.core.money import MONEY_CONTEXT, quantize_amount, to_decimal

SUPPORTED_UNITS = ("hour", "day", "month")


@dataclasses.dataclass(frozen=True, slots=True)
class CostSettings:
    hours_per_day: Decimal
    monthly_rate_divisor: Decimal


def _strict(value: object, name: str) -> Decimal:
    if isinstance(value, float):
        msg = f"{name} must be a Decimal, not float"
        raise TypeError(msg)
    return to_decimal(value)  # type: ignore[arg-type]


def daily_cost(
    pay_rate: Decimal | None, rate_unit: str | None, currency: str, settings: CostSettings
) -> Decimal | None:
    """Cost per bench day, or ``None`` when the rate / unit cannot be turned into a day (``cost_unknown``)."""
    if pay_rate is None or rate_unit not in SUPPORTED_UNITS:
        return None
    rate = _strict(pay_rate, "pay_rate")
    if rate_unit == "hour":
        value = MONEY_CONTEXT.multiply(rate, _strict(settings.hours_per_day, "hours_per_day"))
    elif rate_unit == "day":
        value = rate
    else:
        value = MONEY_CONTEXT.divide(rate, _strict(settings.monthly_rate_divisor, "monthly_rate_divisor"))
    return quantize_amount(value, currency)


def basis_label(rate_unit: str | None, settings: CostSettings) -> str | None:
    """Audit trail of how the cost was derived (``hourx8.00``, ``day``, ``month÷22.00``)."""
    if rate_unit == "hour":
        return f"hourx{settings.hours_per_day}"
    if rate_unit == "day":
        return "day"
    if rate_unit == "month":
        return f"month÷{settings.monthly_rate_divisor}"
    return None


def _weekdays_between(start: date, end: date) -> int:
    """Mon-Fri days in ``[start, end]`` (inclusive); 0 when ``end < start``."""
    if end < start:
        return 0
    total = (end - start).days + 1
    weeks, rest = divmod(total, 7)
    count = weeks * 5
    for i in range(rest):
        if (start + timedelta(days=weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def bench_days(available_from: date, until: date, basis: str = "weekdays") -> int:
    if available_from > until:
        return 0
    if basis == "calendar":
        return (until - available_from).days + 1
    return _weekdays_between(available_from, until)


def cost_to_date(cost_per_day: Decimal | None, days: int, currency: str | None) -> Decimal | None:
    if cost_per_day is None or currency is None:
        return None
    return quantize_amount(
        MONEY_CONTEXT.multiply(_strict(cost_per_day, "cost_per_day"), Decimal(days)), currency
    )


def _month_end(d: date) -> date:
    return d.replace(day=_calendar.monthrange(d.year, d.month)[1])


def month_spans(available_from: date, until: date) -> Iterable[tuple[str, date, date]]:
    """``(yyyy-mm, first day, last day)`` of each calendar month the bench span touches."""
    cur = available_from
    while cur <= until:
        end = min(_month_end(cur), until)
        yield f"{cur.year:04d}-{cur.month:02d}", cur, end
        cur = end + timedelta(days=1)


def cost_by_month(
    cost_per_day: Decimal, currency: str, available_from: date, until: date, basis: str = "weekdays"
) -> dict[str, Decimal]:
    """Each entry-month product is quantized separately (the sum may differ from one total product by at most
    half a minor unit per month)."""
    rate = _strict(cost_per_day, "cost_per_day")
    out: dict[str, Decimal] = {}
    for key, start, end in month_spans(available_from, until):
        days = bench_days(start, end, basis)
        out[key] = quantize_amount(MONEY_CONTEXT.multiply(rate, Decimal(days)), currency)
    return out
