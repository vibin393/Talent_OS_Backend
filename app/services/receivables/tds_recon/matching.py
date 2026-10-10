# ruff: noqa: E501
"""Pure, deterministic 26AS line <-> TDS Deduction matching (Epic 51, FR-254).

Candidates (filtered by the caller): deductions of the Legal Entity whose ``client_id`` is mapped to the line's
deductor TAN, not reversed, ``deduction_date`` in the same FY. Score table:

* section equal (normalised)                          +40
* ``|inr_amount - tds_deducted| <= 1.00``             +40 (exact: +45)
* ``deduction_date`` within +/-45 days of the line    +15
* same quarter                                        +5     (capped at 100)

The best candidate with score >= 80 is proposed per line, greedily by score, then date distance, then stable ids;
each deduction and each line is used once. Proposals are suggestions only: confirming is human-only (FR-257).
"""

import dataclasses
import uuid
from datetime import date
from decimal import Decimal

from app.services.receivables.tds_recon.common import normalise_section, quarter_of

THRESHOLD = 80
DATE_WINDOW_DAYS = 45
AMOUNT_TOLERANCE = Decimal("1.00")


@dataclasses.dataclass(frozen=True, slots=True)
class LineIn:
    id: uuid.UUID
    client_id: uuid.UUID | None  # via the TAN mapping; None = unmapped (never proposed)
    section: str
    transaction_date: date
    tds_deducted: Decimal


@dataclasses.dataclass(frozen=True, slots=True)
class DeductionIn:
    id: uuid.UUID
    client_id: uuid.UUID
    section_code: str | None
    inr_amount: Decimal
    deduction_date: date


@dataclasses.dataclass(frozen=True, slots=True)
class Score:
    value: int
    reasons: tuple[str, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class Proposal:
    line_id: uuid.UUID
    deduction_id: uuid.UUID
    score: int
    reasons: tuple[str, ...]


def score(line: LineIn, ded: DeductionIn) -> Score:
    points = 0
    reasons: list[str] = []
    if ded.section_code and normalise_section(ded.section_code) == normalise_section(line.section):
        points += 40
        reasons.append("section_match")
    diff = abs(Decimal(ded.inr_amount) - Decimal(line.tds_deducted))
    if diff == 0:
        points += 45
        reasons.append("amount_exact")
    elif diff <= AMOUNT_TOLERANCE:
        points += 40
        reasons.append("amount_within_1")
    if abs((ded.deduction_date - line.transaction_date).days) <= DATE_WINDOW_DAYS:
        points += 15
        reasons.append("date_within_45_days")
    if quarter_of(ded.deduction_date) == quarter_of(line.transaction_date):
        points += 5
        reasons.append("same_quarter")
    return Score(min(points, 100), tuple(reasons))


def propose(lines: list[LineIn], deductions: list[DeductionIn]) -> list[Proposal]:
    pairs: list[tuple[int, int, str, str, LineIn, DeductionIn, Score]] = []
    for ln in lines:
        if ln.client_id is None:
            continue
        for d in deductions:
            if d.client_id != ln.client_id:
                continue
            s = score(ln, d)
            if s.value >= THRESHOLD:
                distance = abs((d.deduction_date - ln.transaction_date).days)
                pairs.append((-s.value, distance, str(ln.id), str(d.id), ln, d, s))
    pairs.sort(key=lambda p: p[:4])
    used_lines: set[uuid.UUID] = set()
    used_deds: set[uuid.UUID] = set()
    out: list[Proposal] = []
    for _neg, _dist, _l, _d, ln, d, s in pairs:
        if ln.id in used_lines or d.id in used_deds:
            continue
        used_lines.add(ln.id)
        used_deds.add(d.id)
        out.append(Proposal(ln.id, d.id, s.value, s.reasons))
    return sorted(out, key=lambda p: str(p.line_id))
