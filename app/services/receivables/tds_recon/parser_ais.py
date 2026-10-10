# ruff: noqa: E501
"""Pure parser of the AIS (Annual Information Statement) TDS/TCS CSV export -- Epic 51, FR-254.

Header rows above the table may carry ``PAN,<pan>`` and ``Assessment Year,<ay>`` (or ``Financial Year``). The table
header is the first row naming a TAN and a TDS-deducted column; columns are found through ``HEADER_MAP`` aliases
(case / spacing insensitive). A TAN may also come embedded in the information source (``NAME (BLRA12345B)``).
"""

import csv
import io
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

HEADER_MAP: dict[str, tuple[str, ...]] = {
    "tan": ("tan of deductor", "deductor tan", "tan", "tan of deductor/collector"),
    "name": ("name of deductor", "deductor name", "information source", "name of deductor/collector"),
    "section": ("section", "section code", "information code"),
    "date": (
        "date of payment/credit",
        "transaction date",
        "date of payment",
        "date of credit",
        "payment date",
    ),
    "paid": ("amount paid/credited", "amount paid", "amount", "amount paid/credited (rs.)"),
    "deducted": ("tds deducted", "tax deducted", "tds amount", "tds deducted (rs.)", "tax deducted (rs.)"),
    "deposited": ("tds deposited", "tax deposited", "tds deposited (rs.)"),
    "status": ("status", "booking status", "status of booking"),
    "sr": ("sr. no.", "sr no", "sr.no.", "s.no", "sr"),
}
_EMBEDDED_TAN = re.compile(r"\(([A-Z]{4}[0-9]{5}[A-Z])\)")


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _columns(header: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    normalised = [_norm(h) for h in header]
    for key, aliases in HEADER_MAP.items():
        for j, h in enumerate(normalised):
            if h in aliases:
                found.setdefault(key, j)
                break
    return found


def parse(data: str) -> ParsedStatement:
    rows = list(csv.reader(io.StringIO(data)))
    if not rows:
        raise StatementError("tds_statement_unreadable", "The AIS file is empty")
    pan: str | None = None
    ay: str | None = None
    header_at: int | None = None
    cols: dict[str, int] = {}
    for i, row in enumerate(rows):
        cells = [c.strip() for c in row]
        if len(cells) >= 2:
            label = _norm(cells[0])
            if label in (
                "pan",
                "permanent account number",
                "permanent account number (pan)",
            ) and PAN_RE.match(cells[1].upper()):
                pan = cells[1].upper()
            elif label == "assessment year":
                ay = cells[1]
            elif label == "financial year" and ay is None and re.match(r"^\d{4}-\d{2}$", cells[1]):
                start = int(cells[1][:4]) + 1
                ay = f"{start}-{(start + 1) % 100:02d}"
        candidate = _columns(cells)
        if "deducted" in candidate and ("tan" in candidate or "name" in candidate) and "date" in candidate:
            header_at, cols = i, candidate
            break
    if header_at is None:
        raise StatementError("tds_statement_unreadable", "No TDS table header was found in the AIS file")

    lines: list[ParsedLine] = []
    errors: list[ParseError] = []
    for i, row in enumerate(rows[header_at + 1 :], start=header_at + 2):
        cells = [c.strip() for c in row]
        if not any(cells):
            continue

        def cell(key: str, cells: list[str] = cells) -> str:
            j = cols.get(key)
            return cells[j] if j is not None and j < len(cells) else ""

        name = cell("name")
        tan = cell("tan").upper()
        if not TAN_RE.match(tan):
            m = _EMBEDDED_TAN.search(name.upper())
            tan = m.group(1) if m else tan
        txn_date = parse_date(cell("date"))
        paid, deducted = parse_amount(cell("paid")), parse_amount(cell("deducted"))
        problems = []
        if not TAN_RE.match(tan):
            problems.append(f"no valid deductor TAN ({tan or '-'})")
        if txn_date is None:
            problems.append(f"unreadable date {cell('date')!r}")
        if deducted is None:
            problems.append(f"unreadable TDS deducted {cell('deducted')!r}")
        if problems:
            errors.append(ParseError(i, "; ".join(problems)))
            continue
        assert txn_date is not None and deducted is not None
        status = cell("status").upper()[:1] or None
        lines.append(
            ParsedLine(
                line_no=i,
                line_ref=f"{tan}-{cell('sr') or i}",
                deductor_tan=tan,
                deductor_name=_EMBEDDED_TAN.sub("", name).strip()[:200] or tan,
                section=(cell("section") or "-")[:10],
                transaction_date=txn_date,
                amount_paid=paid if paid is not None else deducted,
                tds_deducted=deducted,
                tds_deposited=parse_amount(cell("deposited")),
                booking_status=status if status in ("F", "U", "P", "O", "M", "Z") else None,
            )
        )
    if not lines and not errors:
        raise StatementError("tds_statement_unreadable", "No TDS rows were found in the AIS file")
    return ParsedStatement(pan=pan, assessment_year=ay, lines=tuple(lines), errors=tuple(errors))
