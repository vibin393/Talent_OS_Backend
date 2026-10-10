"""Risk bands (Epic 48 A5): by tenant threshold for ``offer_drop`` / ``attrition`` (defaults medium 0.25, high
0.50); none for ``time_to_fill``."""

from decimal import Decimal


def band_for(probability: float, thresholds: tuple[Decimal, Decimal] | None) -> str | None:
    if thresholds is None:
        return None
    medium, high = thresholds
    p = Decimal(str(probability))
    if p >= high:
        return "high"
    if p >= medium:
        return "medium"
    return "low"
