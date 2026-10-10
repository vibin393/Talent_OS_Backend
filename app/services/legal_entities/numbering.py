"""Gap-free statutory document numbering (AD-18).

Only ``assign`` produces Invoice / Credit Note / Debit Note numbers, inside the caller's issue transaction:
the per (series, financial year) counter row is created if missing (``INSERT ... ON CONFLICT DO NOTHING``),
locked ``FOR UPDATE`` and advanced; a rollback of the issue transaction returns the number, so committed
numbers are gap-free and duplicate-free.

Lock order (binding for every issue path): document row -> original Invoice row (notes) -> PO row(s) in uuid
order -> posting rows -> series counter **last**. No external I/O may happen after the counter is taken (render,
email and connector calls run after commit via events). ``assign`` never commits.
"""

import dataclasses
import uuid
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, fiscal
from app.core.errors import ConflictError, FieldError, NotFound, ValidationFailed
from app.models.legal_entities import DocumentSeries

MAX_LENGTH = 16
_ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/-")
_FY_TOKEN_LENGTH = {"YY-YY": 5, "YYYY-YY": 7, "YY": 2, "none": 0}


@dataclasses.dataclass(frozen=True, slots=True)
class SeriesConfig:
    prefix: str
    fy_format: str
    separator: str
    pad: int


def _config(series: "DocumentSeries | SeriesConfig") -> SeriesConfig:
    return SeriesConfig(series.prefix or "", series.fy_format, series.separator or "", int(series.pad))


def fy_token(fy_format: str, fiscal_year_: str) -> str:
    start, _ = fiscal.fiscal_year_bounds(fiscal_year_)
    if fy_format == "YY-YY":
        return fiscal_year_
    if fy_format == "YYYY-YY":
        return f"{start.year}-{fiscal_year_[3:]}"
    if fy_format == "YY":
        return fiscal_year_[:2]
    if fy_format == "none":
        return ""
    msg = f"unknown fy_format {fy_format!r}"
    raise ValueError(msg)


def render(series: "DocumentSeries | SeriesConfig", fiscal_year: str, value: int) -> str:
    cfg = _config(series)
    parts = [p for p in (cfg.prefix, fy_token(cfg.fy_format, fiscal_year)) if p]
    parts.append(f"{value:0{cfg.pad}d}")
    return cfg.separator.join(parts)


def max_rendered_length(series_like: SeriesConfig) -> int:
    cfg = _config(series_like)
    parts = [len(cfg.prefix)] if cfg.prefix else []
    if _FY_TOKEN_LENGTH.get(cfg.fy_format):
        parts.append(_FY_TOKEN_LENGTH[cfg.fy_format])
    parts.append(cfg.pad)
    return sum(parts) + len(cfg.separator) * (len(parts) - 1)


def validate_series_config(cfg: SeriesConfig) -> list[FieldError]:
    errors: list[FieldError] = []
    if any(ch not in _ALLOWED for ch in (cfg.prefix or "") + (cfg.separator or "")):
        errors.append(
            FieldError("prefix", "series_bad_chars", "Only letters, digits, '/' and '-' are allowed in numbers")
        )
    if cfg.fy_format not in _FY_TOKEN_LENGTH:
        errors.append(FieldError("fy_format", "series_bad_format", "Unknown financial-year format"))
    elif max_rendered_length(cfg) > MAX_LENGTH:
        errors.append(
            FieldError("prefix", "series_too_long", f"Numbers of this series could exceed {MAX_LENGTH} characters")
        )
    if not 1 <= int(cfg.pad) <= 10:
        errors.append(FieldError("pad", "series_bad_pad", "Padding must be between 1 and 10"))
    return errors


def _tenant(session: AsyncSession) -> uuid.UUID:
    raw = session.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "numbering needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


async def _series(session: AsyncSession, series_id: uuid.UUID) -> DocumentSeries:
    series = (
        await session.execute(select(DocumentSeries).where(DocumentSeries.id == series_id))
    ).scalar_one_or_none()
    if series is None:
        raise NotFound("Numbering series not found")
    return series


_ENSURE_COUNTER = text(
    "INSERT INTO document_series_counters (tenant_id, series_id, fiscal_year, last_value) "
    "VALUES (:tenant_id, :series_id, :fy, 0) ON CONFLICT (series_id, fiscal_year) DO NOTHING"
)
_LOCK_COUNTER = text(
    "SELECT last_value, last_doc_date FROM document_series_counters "
    "WHERE series_id = :series_id AND fiscal_year = :fy FOR UPDATE"
)
_ADVANCE = text(
    "UPDATE document_series_counters SET last_value = last_value + 1, "
    "last_doc_date = GREATEST(COALESCE(last_doc_date, :doc_date), :doc_date) "
    "WHERE series_id = :series_id AND fiscal_year = :fy RETURNING last_value"
)


async def assign(session: AsyncSession, series_id: uuid.UUID, doc_date: date) -> str:
    """Assign the next number of ``series_id`` for ``doc_date``'s financial year (counter locked last)."""
    series = await _series(session, series_id)
    if series.status != "active":
        raise ConflictError("This numbering series is not active", code="series_inactive")
    fy = fiscal.fiscal_year(doc_date)
    params = {"tenant_id": _tenant(session), "series_id": series_id, "fy": fy, "doc_date": doc_date}
    await session.execute(_ENSURE_COUNTER, params)
    row = (await session.execute(_LOCK_COUNTER, params)).one()
    if row.last_doc_date is not None and doc_date < row.last_doc_date and not series.allow_backdated:
        raise ValidationFailed(
            "The document date is before the last issued document of this series",
            code="doc_date_before_last_issued",
            details={"last_doc_date": row.last_doc_date.isoformat(), "doc_date": doc_date.isoformat()},
        )
    value = int((await session.execute(_ADVANCE, params)).scalar_one())
    number = render(series, fy, value)
    if len(number) > MAX_LENGTH:
        raise ConflictError("This numbering series is exhausted", code="series_exhausted")
    audit.record("number_assigned", "document_series", series_id, session=session, fiscal_year=fy, number=number)
    return number


async def preview_next(session: AsyncSession, series_id: uuid.UUID, doc_date: date) -> str:
    """The number ``assign`` would return now (no lock; display only, never use it for a document)."""
    series = await _series(session, series_id)
    fy = fiscal.fiscal_year(doc_date)
    last = (
        await session.execute(
            text(
                "SELECT last_value FROM document_series_counters WHERE series_id = :series_id AND fiscal_year = :fy"
            ),
            {"series_id": series_id, "fy": fy},
        )
    ).scalar_one_or_none()
    return render(series, fy, int(last or 0) + 1)


async def raise_counter_to(session: AsyncSession, series_id: uuid.UUID, fiscal_year: str, value: int) -> None:
    """FR-185 imports: make the next number ``value + 1`` at least (under the counter lock)."""
    await _series(session, series_id)
    fiscal.fiscal_year_bounds(fiscal_year)  # validates the token
    params = {"tenant_id": _tenant(session), "series_id": series_id, "fy": fiscal_year, "value": int(value)}
    await session.execute(_ENSURE_COUNTER, params)
    await session.execute(_LOCK_COUNTER, params)
    await session.execute(
        text(
            "UPDATE document_series_counters SET last_value = :value "
            "WHERE series_id = :series_id AND fiscal_year = :fy AND last_value < :value"
        ),
        params,
    )


async def counters_in_use(session: AsyncSession, series_id: uuid.UUID) -> bool:
    row = (
        await session.execute(
            text("SELECT 1 FROM document_series_counters WHERE series_id = :sid AND last_value > 0 LIMIT 1"),
            {"sid": series_id},
        )
    ).first()
    return row is not None
