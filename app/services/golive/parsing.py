"""CSV decoding for go-live imports: UTF-8 with or without BOM, header row required; row numbers are the
1-based data-row index (row 1 = the first line after the header)."""

import csv
import io

from app.core.errors import ValidationFailed
from app.services.golive.templates import KindDef


def _unsupported() -> ValidationFailed:
    return ValidationFailed(
        "Upload a UTF-8 CSV file", code="unsupported_file_type", details={"allowed": ["csv"]}
    )


def decode(data: bytes) -> str:
    if b"\x00" in data[:4096]:
        raise _unsupported()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _unsupported() from exc


def parse(data: bytes, k: KindDef, *, max_rows: int) -> list[dict[str, str]]:
    """Return one ``{column: value}`` dict per non-blank data row (unknown columns are dropped)."""
    text = decode(data)
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValidationFailed("The file is empty", code="empty_file") from exc
    except csv.Error as exc:
        raise _unsupported() from exc
    names = [h.strip().lower() for h in header]
    missing = [c for c in k.required if c not in names]
    if missing:
        raise ValidationFailed(
            "Required columns are missing", code="missing_columns", details={"columns": missing}
        )
    known = set(k.headers)
    rows: list[dict[str, str]] = []
    try:
        for raw in reader:
            if not any(cell.strip() for cell in raw):
                continue
            if len(rows) >= max_rows:
                raise ValidationFailed(
                    f"The file has more than {max_rows} rows",
                    code="too_many_rows",
                    details={"max_rows": max_rows},
                )
            rows.append(
                {
                    name: (raw[i].strip() if i < len(raw) else "")
                    for i, name in enumerate(names)
                    if name in known
                }
            )
    except csv.Error as exc:
        raise _unsupported() from exc
    if not rows:
        raise ValidationFailed("The file has no data rows", code="empty_file")
    return rows


def mask(value: str) -> str:
    tail = (value or "").strip()[-4:]
    return f"****{tail}" if tail else ""
