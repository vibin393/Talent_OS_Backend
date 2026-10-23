# ruff: file-ignore[line-too-long]
"""Conversion fee (FR-164), pure and Decimal-only (AD-20).

``compute(rule, offered_compensation, *, months_served=None) -> Decimal | None``:

* ``pct_of_ctc`` (legacy name ``percent_annual_comp``): quantize(comp x pct / 100) to the currency's minor units,
  half-up;
* ``fixed``: the rule's value;
* ``none``: ``None``. A ``waive_after_months`` the Worker has reached also gives ``None``.
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from app.core.money import quantize_amount

_HUNDRED = Decimal(100)
PCT_KINDS = frozenset({"pct_of_ctc", "percent_annual_comp"})


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, float):
        msg = "floats are not money"
        raise TypeError(msg)
    return value if isinstance(value, Decimal) else Decimal(str(value))


def months_between(start: Any, end: Any) -> int:
    """Whole months from ``start`` to ``end`` (dates); 0 when either is missing or end precedes start."""
    if start is None or end is None or end < start:
        return 0
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(months, 0)  # type: ignore[no-any-return]


def compute(
    rule: Mapping[str, Any] | None,
    offered_compensation: Decimal | str | int,
    *,
    currency: str = "INR",
    months_served: int | None = None,
) -> Decimal | None:
    if not rule:
        return None
    kind = rule.get("kind") or "none"
    waive = rule.get("waive_after_months")
    if waive is not None and months_served is not None and months_served >= int(waive):
        return None
    value = _dec(rule.get("value"))
    if kind in PCT_KINDS:
        comp = _dec(offered_compensation)
        if comp is None or value is None:
            return None
        return quantize_amount(comp * value / _HUNDRED, currency)
    if kind == "fixed":
        return None if value is None else quantize_amount(value, currency)
    return None
