"""Shared helpers of the Phase 3 tile functions (Epic 52, A4).

* ``need`` raises ``NotAvailable`` for a missing optional table (``p3tables``) -> the tile is ``unavailable``.
* Money is the AD-12 shape ``{"amount": "...", "currency": "..."}``; compensation detail is omitted without
  ``compensation:view`` (counts stay).
* Drill-down links go to the owner's list route with the same filters; ``None`` when the route does not exist.
"""

from decimal import Decimal
from typing import Any

from app.core.permissions import can_view_compensation
from app.services.reporting.metrics import TileContext, link
from app.services.reporting.metrics.p2common import need as need  # ruff: ignore[unused-import] - re-exported

SUPPORTING_SIGNAL = "Supporting signal"


def comp(t: TileContext) -> bool:
    return can_view_compensation(t.principal)


def money(amount: Any, currency: str | None) -> dict[str, str | None] | None:
    if amount is None:
        return None
    return {"amount": f"{Decimal(amount):.2f}", "currency": currency}


def range_link(t: TileContext, path: str, **query: Any) -> dict[str, Any]:
    return link(path, {"from": t.filters.from_date.isoformat(), "to": t.filters.to_date.isoformat(), **query})


def label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return round(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2, 1)


def days_between(a: Any, b: Any) -> float:
    """Days from ``a`` to ``b`` (datetimes: fractional; dates: whole days)."""
    if hasattr(a, "hour") and hasattr(b, "hour"):
        return (b - a).total_seconds() / 86400.0
    da = a.date() if hasattr(a, "hour") else a
    dbb = b.date() if hasattr(b, "hour") else b
    return float((dbb - da).days)
