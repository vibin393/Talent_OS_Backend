# ruff: file-ignore[line-too-long]
"""TDS / GST-TDS arithmetic (FR-154). Pure: no I/O.

The base is the taxable value excluding GST (pure-agent amounts and RCM tax are never part of it).
"""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def expected_tds(taxable_excl_gst: Decimal, rate_pct: Decimal) -> Decimal:
    return (Decimal(taxable_excl_gst) * Decimal(rate_pct) / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)


def variance(expected: Decimal, recorded: Decimal, tolerance: Decimal) -> bool:
    """Return True when ``recorded`` differs from ``expected`` by more than ``tolerance``."""
    return abs(Decimal(recorded) - Decimal(expected)) > Decimal(tolerance)
