"""Rate matching (Epic 16 A6, FR-91). Pure: no I/O, deterministic.

``match_line(lines, q)`` picks the most specific matching rate card line; ``evaluate(match, q)`` grades the
requested bill / vendor rate against it.

Matching: ``role_key`` equal (exact, normalised), the line's location equal or NULL (= any), the experience
within the band (NULL bounds open; an unknown experience only matches open bands), ``currency`` and
``rate_unit`` equal. Specificity: a located line beats an "any location" line; then the narrower experience
band (open bands count as infinitely wide); ties -> lowest ``min_bill_rate``, then the line id.
"""

import dataclasses
import re
import uuid
from collections.abc import Iterable
from decimal import Decimal
from typing import Literal

Result = Literal["ok", "out_of_range", "no_matching_line", "vendor_rate_exceeded"]
_SPACES = re.compile(r"\s+")
_INFINITE = Decimal("1E9")


def normalise(value: str | None) -> str | None:
    """Lower-case, trimmed, collapsed spaces (``role_key`` / ``location_key``)."""
    if value is None:
        return None
    text = _SPACES.sub(" ", value.strip().lower())
    return text or None


@dataclasses.dataclass(frozen=True, slots=True)
class Line:
    id: uuid.UUID
    role_key: str
    location_key: str | None
    experience_min_years: Decimal | None
    experience_max_years: Decimal | None
    min_bill_rate: Decimal
    max_bill_rate: Decimal
    currency: str
    rate_unit: str
    max_vendor_rate: Decimal | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class RateQuery:
    role: str | None
    location: str | None
    experience_years: Decimal | None
    currency: str
    rate_unit: str
    bill_rate: Decimal | None = None
    vendor_rate: Decimal | None = None


def _experience_ok(line: Line, years: Decimal | None) -> bool:
    if years is None:
        return line.experience_min_years is None and line.experience_max_years is None
    if line.experience_min_years is not None and years < line.experience_min_years:
        return False
    return not (line.experience_max_years is not None and years > line.experience_max_years)


def _band_width(line: Line) -> Decimal:
    if line.experience_min_years is None or line.experience_max_years is None:
        return _INFINITE
    return line.experience_max_years - line.experience_min_years


def candidates(lines: Iterable[Line], q: RateQuery) -> list[Line]:
    role = normalise(q.role)
    location = normalise(q.location)
    if role is None:
        return []
    return [
        line
        for line in lines
        if line.role_key == role
        and (line.location_key is None or line.location_key == location)
        and _experience_ok(line, q.experience_years)
        and line.currency.upper() == q.currency.upper()
        and line.rate_unit == q.rate_unit
    ]


def _rank(line: Line) -> tuple[int, Decimal, Decimal, str]:
    return (0 if line.location_key is not None else 1, _band_width(line), line.min_bill_rate, str(line.id))


def match_line(lines: Iterable[Line], q: RateQuery) -> Line | None:
    found = candidates(lines, q)
    return min(found, key=_rank) if found else None


def evaluate(match: Line | None, q: RateQuery) -> Result:
    if match is None:
        return "no_matching_line"
    if q.bill_rate is not None and not (match.min_bill_rate <= q.bill_rate <= match.max_bill_rate):
        return "out_of_range"
    if (
        q.vendor_rate is not None
        and match.max_vendor_rate is not None
        and q.vendor_rate > match.max_vendor_rate
    ):
        return "vendor_rate_exceeded"
    return "ok"


STATUS_OF: dict[str, str] = {
    "ok": "within",
    "out_of_range": "outside",
    "vendor_rate_exceeded": "outside",
    "no_matching_line": "no_match",
    "not_validated": "not_applicable",
}


def needs_exception(result: str) -> bool:
    return result in ("out_of_range", "vendor_rate_exceeded", "no_matching_line")
