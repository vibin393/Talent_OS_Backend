# ruff: noqa: E501
"""Pure parser of the TRACES Form 26AS text export (``^``-delimited) -- Epic 51, FR-254.

Layout (anonymised, real-shaped):

* an assessee header: a row naming the columns (``... ^Permanent Account Number (PAN)^ ... ^Assessment Year^ ...``)
  followed by the values row;
* ``PART-I`` / ``PART A`` (TDS): deductor rows ``Sr^Name^TAN^Total paid^Total deducted^Total deposited`` (extra
  empty cells tolerated), each followed by its transaction rows
  ``[^]Sr^Section^Transaction date^Booking status^Booking date^Remarks^Amount paid^Tax deducted^TDS deposited``
  (dates ``DD-Mon-YYYY``, amounts with commas);
* other parts (``PART-II`` ...) are ignored.

A transaction row that cannot be read becomes a line-level error (``ParseError``), never a silent drop.
"""

import re

from app.services.receivables.tds_recon.common import (
    PAN_RE,
    TAN_RE,
    ParsedLine,
    ParsedStatement,
    ParseError,
    StatementError,
    parse_amount,
    parse_date,
)

_PART_RE = re.compile(r"^\s*PART[\s-]*([A-Z0-9]+)\b", re.IGNORECASE)
_TDS_PARTS = {"I", "A", "1"}


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.rstrip("\r\n").split("^")]


def _header_values(rows: list[list[str]]) -> tuple[str | None, str | None]:
    """PAN and assessment year from the column-header row + the next row; falls back to the first PAN token."""
    for i, cells in enumerate(rows[:-1]):
        lowered = [c.lower() for c in cells]
        pan_col = next(
            (j for j, c in enumerate(lowered) if "permanent account number" in c or c == "pan"), None
        )
        if pan_col is None:
            continue
        values = rows[i + 1]
        pan = values[pan_col].upper() if pan_col < len(values) else None
        ay_col = next((j for j, c in enumerate(lowered) if "assessment year" in c), None)
        ay = values[ay_col] if ay_col is not None and ay_col < len(values) else None
        return (pan if pan and PAN_RE.match(pan) else None), (ay or None)
    for cells in rows[:40]:
        for c in cells:
            if PAN_RE.match(c.upper()):
                return c.upper(), None
    return None, None


def _is_int(value: str) -> bool:
    return value.isdigit()


def parse(text: str) -> ParsedStatement:
    raw_lines = text.splitlines()
    rows = [_cells(ln) for ln in raw_lines]
    if not any("^" in ln for ln in raw_lines):
        raise StatementError(
            "tds_statement_unreadable", "This is not a TRACES 26AS text export ('^'-delimited)"
        )
    pan, ay = _header_values(rows)

    has_parts = any(_PART_RE.match(ln) for ln in raw_lines)
    in_tds = not has_parts
    deductor: tuple[str, str, str] | None = None  # (sr, name, tan)
    lines: list[ParsedLine] = []
    errors: list[ParseError] = []
    for idx, (raw, cells) in enumerate(zip(raw_lines, rows, strict=True), start=1):
        part = _PART_RE.match(raw)
        if part:
            in_tds = part.group(1).upper() in _TDS_PARTS
            deductor = None
            continue
        if not in_tds or not raw.strip():
            continue
        lowered = raw.lower()
        if "sr. no" in lowered or lowered.startswith("sr^") or "name of deductor" in lowered:
            continue  # column header rows
        lead_empty = cells[0] == ""
        body = cells[1:] if lead_empty else cells
        if not body or not _is_int(body[0]):
            continue  # totals / notes rows
        # deductor row: Sr^Name^TAN^...
        if not lead_empty and len(body) >= 3 and TAN_RE.match(body[2].upper()):
            deductor = (body[0], body[1], body[2].upper())
            continue
        # transaction row: Sr^Section^Date^Status^BookingDate^Remarks^Paid^Deducted^Deposited
        if deductor is None:
            errors.append(ParseError(idx, "transaction row before any deductor row"))
            continue
        if len(body) < 9:
            errors.append(ParseError(idx, f"expected 9 columns, found {len(body)}"))
            continue
        section = body[1]
        txn_date = parse_date(body[2])
        status = (body[3] or "").strip().upper()[:1] or None
        paid, deducted, deposited = parse_amount(body[6]), parse_amount(body[7]), parse_amount(body[8])
        problems = []
        if not section:
            problems.append("section missing")
        if txn_date is None:
            problems.append(f"unreadable transaction date {body[2]!r}")
        if paid is None:
            problems.append(f"unreadable amount paid {body[6]!r}")
        if deducted is None:
            problems.append(f"unreadable tax deducted {body[7]!r}")
        if problems:
            errors.append(ParseError(idx, "; ".join(problems)))
            continue
        assert txn_date is not None and paid is not None and deducted is not None
        lines.append(
            ParsedLine(
                line_no=idx,
                line_ref=f"{deductor[2]}-{deductor[0]}-{body[0]}",
                deductor_tan=deductor[2],
                deductor_name=deductor[1][:200],
                section=section[:10],
                transaction_date=txn_date,
                amount_paid=paid,
                tds_deducted=deducted,
                tds_deposited=deposited,
                booking_status=status,
            )
        )
    if not lines and not errors:
        raise StatementError("tds_statement_unreadable", "No TDS transactions were found in the statement")
    return ParsedStatement(pan=pan, assessment_year=ay, lines=tuple(lines), errors=tuple(errors))
