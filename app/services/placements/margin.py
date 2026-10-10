"""Margin (PRD §3 amended), pure and Decimal-only (AD-20).

* Cost = Pay Rate x (1 + burden%) for a direct worker, or Vendor Rate x (1 + vendor burden%) when a vendor rate is
  given (vendor-supplied worker).
* Margin = Bill Rate - Cost, rounded half-up to the currency's minor units; Margin % = Margin / Bill Rate x 100,
  rounded half-up to 4 places. Without a bill rate (or a cost) there is no margin.
"""

from decimal import ROUND_HALF_UP, Decimal

from app.core.money import quantize_amount

_HUNDRED = Decimal(100)
_PCT_Q = Decimal("0.0001")


def _d(value: Decimal | str | int | None) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, float):  # pragma: no cover - guarded by callers
        msg = "floats are not money"
        raise TypeError(msg)
    return value if isinstance(value, Decimal) else Decimal(str(value))


def cost(
    pay_rate: Decimal | None,
    burden_pct: Decimal | None = None,
    vendor_rate: Decimal | None = None,
    vendor_burden_pct: Decimal | None = None,
) -> Decimal | None:
    vendor_rate, pay_rate = _d(vendor_rate), _d(pay_rate)
    if vendor_rate is not None:
        return vendor_rate * (1 + (_d(vendor_burden_pct) or Decimal(0)) / _HUNDRED)
    if pay_rate is not None:
        return pay_rate * (1 + (_d(burden_pct) or Decimal(0)) / _HUNDRED)
    return None


def compute(
    *,
    bill_rate: Decimal | str | None,
    pay_rate: Decimal | str | None = None,
    burden_pct: Decimal | str | None = None,
    vendor_rate: Decimal | str | None = None,
    vendor_burden_pct: Decimal | str | None = None,
    currency: str = "INR",
) -> tuple[Decimal | None, Decimal | None]:
    """``(margin_amount, margin_pct)``; ``(None, None)`` when not computable."""
    bill = _d(bill_rate)
    c = cost(_d(pay_rate), _d(burden_pct), _d(vendor_rate), _d(vendor_burden_pct))
    if bill is None or c is None:
        return None, None
    amount = quantize_amount(bill - c, currency)
    pct = None if bill == 0 else ((bill - c) / bill * _HUNDRED).quantize(_PCT_Q, rounding=ROUND_HALF_UP)
    return amount, pct
