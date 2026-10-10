# ruff: noqa: E501
"""Pure shared helpers of the 26AS / AIS reconciliation (Epic 51, FR-254): parsed-statement types, date /
amount parsing, FY / quarter arithmetic and the normalised ``line_hash``."""

import dataclasses
import hashlib
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
TAN_RE = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")
FY_RE = re.compile(r"^(\d{4})-(\d{2})$")
_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d %b %Y")


class StatementError(Exception):
    """A whole-file problem (422 with ``code``)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclasses.dataclass(frozen=True, slots=True)
class ParsedLine:
    line_no: int
    line_ref: str
    deductor_tan: str
    deductor_name: str
    section: str
    transaction_date: date
    amount_paid: Decimal
    tds_deducted: Decimal
    tds_deposited: Decimal | None
    booking_status: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class ParseError:
    line_no: int
    message: str


@dataclasses.dataclass(frozen=True, slots=True)
class ParsedStatement:
    pan: str | None
    assessment_year: str | None
    lines: tuple[ParsedLine, ...]
    errors: tuple[ParseError, ...] = ()


def parse_date(raw: str) -> date | None:
    value = (raw or "").strip()
    if not value or value in ("-", "NA"):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(raw: str) -> Decimal | None:
    value = (raw or "").strip().replace(",", "").replace("₹", "").replace("Rs.", "").strip()
    if not value or value in ("-", "NA"):
        return None
    try:
        return Decimal(value).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def fy_of(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def quarter_of(d: date) -> str:
    return {4: "Q1", 5: "Q1", 6: "Q1", 7: "Q2", 8: "Q2", 9: "Q2", 10: "Q3", 11: "Q3", 12: "Q3"}.get(
        d.month, "Q4"
    )


def valid_fy(fy: str) -> bool:
    m = FY_RE.match(fy or "")
    return bool(m) and (int(m.group(1)) + 1) % 100 == int(m.group(2))  # type: ignore[union-attr]


def fy_short(fy: str) -> str:
    """``2026-27`` -> ``26-27`` (the P2 ``tds_deductions.quarter`` prefix)."""
    return f"{fy[2:4]}-{fy[5:7]}"


def fy_from_assessment_year(ay: str | None) -> str | None:
    m = re.match(r"^(\d{4})-(\d{2,4})$", (ay or "").strip())
    if not m:
        return None
    start = int(m.group(1)) - 1
    return f"{start}-{(start + 1) % 100:02d}"


def fy_bounds(fy: str) -> tuple[date, date]:
    start = int(fy[:4])
    return date(start, 4, 1), date(start + 1, 3, 31)


def normalise_section(raw: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (raw or "").upper())


def line_hash(line: ParsedLine, occurrence: int = 0) -> str:
    """sha256 of the normalised (TAN, section, date, amount paid, TDS deducted, occurrence) tuple; booking status and
    refs are excluded so a re-downloaded statement maps to the same hashes."""
    material = "|".join(
        (
            line.deductor_tan.upper(),
            normalise_section(line.section),
            line.transaction_date.isoformat(),
            f"{line.amount_paid:.2f}",
            f"{line.tds_deducted:.2f}",
            str(occurrence),
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


def hashes(lines: list[ParsedLine] | tuple[ParsedLine, ...]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for ln in lines:
        base = line_hash(ln, 0)
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(base if n == 0 else line_hash(ln, n))
    return out


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")
