"""AI demand intake (FR-13, story 4.4; NFR-14).

Pasted text or an uploaded PDF/DOCX becomes a **Draft** demand: the source is stored (storage + scanning),
the AI gateway extracts schema-only fields (``IntakeExtraction``, ``extra`` forbidden), every value is
re-validated here and invalid ones are dropped, and the populated fields are listed in
``ai_extracted_fields`` until a person edits or confirms them. Intake never assigns, submits, publishes or
transitions; instructions inside the document cannot change that (the document only feeds the schema).
"""

import contextlib
import hashlib
import io
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scanning, storage
from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.core.permissions import check
from app.models.demands import EMPLOYMENT_TYPES, RATE_UNITS, WORK_MODES, Demand, DemandDocument
from app.schemas.demands import DemandCreate, IntakeExtraction
from app.services.demands.common import CREATE, principal_of
from app.services.demands.constants import MAX_INTAKE_BYTES, MAX_INTAKE_CHARS, MAX_INTAKE_PAGES
from app.services.demands.jd import ai_unavailable
from app.services.demands.records import _check_client, create_demand_row, demand_out

PDF_DOCX = frozenset({"pdf", "docx"})
UNTITLED = "Untitled demand"

_WORK_MODE_ALIASES = {"on-site": "onsite", "on site": "onsite", "office": "onsite", "wfh": "remote"}
_EMPLOYMENT_ALIASES = {
    "contract-to-hire": "contract_to_hire",
    "contract to hire": "contract_to_hire",
    "c2h": "contract_to_hire",
    "full-time": "permanent",
    "full time": "permanent",
    "fulltime": "permanent",
    "full_time": "permanent",
    "fte": "permanent",
    "contractor": "contract",
    "freelancer": "freelance",
}
_RATE_UNIT_ALIASES = {
    "hourly": "hour",
    "per hour": "hour",
    "hr": "hour",
    "daily": "day",
    "per day": "day",
    "monthly": "month",
    "per month": "month",
    "yearly": "year",
    "annual": "year",
    "annually": "year",
    "per annum": "year",
}


# ================================================================ validation of AI output
def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] if cleaned else None


def _labels(values: Any, limit: int = 50) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values or []:
        label = _text(v, 200)
        if label and label.lower() not in seen:
            seen.add(label.lower())
            out.append(label)
        if len(out) >= limit:
            break
    return out


def _enum(value: Any, allowed: tuple[str, ...], aliases: dict[str, str]) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    key = aliases.get(key, key).replace("-", "_").replace(" ", "_")
    return key if key in allowed else None


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and Decimal(0) <= amount < Decimal("1e12") else None


def _years(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        years = Decimal(str(value)).quantize(Decimal("0.1"))
    except (InvalidOperation, ValueError):
        return None
    return years if years.is_finite() and Decimal(0) <= years <= Decimal(99) else None


def sanitize(ext: IntakeExtraction) -> dict[str, Any]:
    """Return validated field -> value for every usable extracted value (invalid ones are dropped)."""
    out: dict[str, Any] = {}
    if title := _text(ext.title, 300):
        out["title"] = title
    if skills := _labels(ext.mandatory_skills):
        out["mandatory_skills"] = skills
    if skills := [
        s
        for s in _labels(ext.preferred_skills)
        if s.lower() not in {m.lower() for m in out.get("mandatory_skills", [])}
    ]:
        out["preferred_skills"] = skills
    lo, hi = _years(ext.min_experience_years), _years(ext.max_experience_years)
    if lo is not None and hi is not None and lo > hi:
        hi = None
    if lo is not None:
        out["min_experience_years"] = lo
    if hi is not None:
        out["max_experience_years"] = hi
    if isinstance(ext.positions, int) and not isinstance(ext.positions, bool) and 1 <= ext.positions <= 10000:
        out["positions"] = ext.positions
    if location := _text(ext.location, 300):
        out["location"] = location
    if isinstance(ext.country, str) and re.fullmatch(r"[A-Za-z]{2}", ext.country.strip()):
        out["country"] = ext.country.strip().upper()
    if work_mode := _enum(ext.work_mode, WORK_MODES, _WORK_MODE_ALIASES):
        out["work_mode"] = work_mode
    if employment := _enum(ext.employment_type, EMPLOYMENT_TYPES, _EMPLOYMENT_ALIASES):
        out["employment_type"] = employment
    if (bill := _money(ext.bill_rate)) is not None:
        out["bill_rate"] = bill
    pmin, pmax = _money(ext.pay_rate_min), _money(ext.pay_rate_max)
    if pmin is not None and pmax is not None and pmin > pmax:
        pmin = pmax = None
    if pmin is not None:
        out["pay_rate_min"] = pmin
    if pmax is not None:
        out["pay_rate_max"] = pmax
    if isinstance(ext.currency, str) and re.fullmatch(r"[A-Za-z]{3}", ext.currency.strip()):
        out["currency"] = ext.currency.strip().upper()
    if rate_unit := _enum(ext.rate_unit, RATE_UNITS, _RATE_UNIT_ALIASES):
        out["rate_unit"] = rate_unit
    if isinstance(ext.start_date, str):
        with contextlib.suppress(ValueError):
            out["start_date"] = date.fromisoformat(ext.start_date.strip()[:10])
    if certs := _labels(ext.certifications):
        out["certifications"] = certs
    return out


# ================================================================ document handling
def _too_long(detail: str) -> ValidationFailed:
    return ValidationFailed(
        "The document is too long for intake (max 10 pages / 60,000 characters)",
        code="document_too_long",
        details={"reason": detail},
    )


def _pdf_pages(data: bytes) -> int:
    from pypdf import PdfReader

    try:
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:  # unreadable: the extractor reports it
        return 0


def _extract(data: bytes, kind: str) -> str:
    from app.services.candidates.extraction import extract_text

    if kind == "pdf" and _pdf_pages(data) > MAX_INTAKE_PAGES:
        raise _too_long("pages")
    result = extract_text(data, kind)
    if result.status != "ok" or not result.text:
        raise ValidationFailed(
            "The document text could not be read; paste the text instead",
            code="document_unreadable",
            details={"reason": result.status},
        )
    return result.text


async def read_file_bytes(upload: Any) -> bytes:
    data: bytes = await upload.read(MAX_INTAKE_BYTES + 1)
    if len(data) > MAX_INTAKE_BYTES:
        raise ValidationFailed("The file is larger than 10 MB", code="file_too_large", details={"max_mb": 10})
    return data


async def _store_text(ctx: RequestContext, db: AsyncSession, demand: Demand, text: str) -> DemandDocument:  # ruff: ignore[unused-function-argument]
    data = text.encode("utf-8")
    result = await scanning.scan(data, "intake.txt")
    if result.status in ("infected", "error"):
        raise ValidationFailed("The text failed the malware scan", code="scan_unavailable")
    key = storage.new_key(demand.tenant_id)
    await storage.get_storage().put(key, data, "text/plain; charset=utf-8")
    row = DemandDocument(
        id=uuid.uuid4(),
        tenant_id=demand.tenant_id,
        demand_id=demand.id,
        kind="intake_source",
        file_name="intake.txt",
        content_type="text/plain",
        size=len(data),
        storage_key=key,
        scan_status=result.status,
    )
    db.add(row)
    await db.flush()
    logger.info(
        "Stored intake text for demand {} ({} bytes, sha256={})",
        demand.id,
        len(data),
        hashlib.sha256(data).hexdigest()[:12],
    )
    return row


async def _store_file(
    ctx: RequestContext, db: AsyncSession, demand: Demand, data: bytes, filename: str | None
) -> DemandDocument:
    from app.services import files_service

    stored = await files_service.store_upload(ctx, db, data, filename, PDF_DOCX, "demand", demand.id)
    row = DemandDocument(
        id=uuid.uuid4(),
        tenant_id=demand.tenant_id,
        demand_id=demand.id,
        kind="intake_source",
        file_name=stored.original_name,
        content_type=stored.content_type,
        size=stored.size_bytes,
        storage_key=stored.storage_key,
        scan_status=stored.scan_status,
        file_id=stored.id,
    )
    db.add(row)
    await db.flush()
    return row


# ================================================================ entry point
async def intake(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    client_id: uuid.UUID,
    text: str | None = None,
    upload: Any | None = None,
    file_data: bytes | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    """``upload`` is an ``UploadFile`` (read here, capped at 10 MB); tests may pass ``file_data`` directly."""
    from app.services import ai
    from app.services.files_service import sniff_kind

    principal = principal_of(ctx)
    check(principal, CREATE)
    text = text.strip() if text else None
    if upload is not None:
        file_data = await read_file_bytes(upload)
        filename = getattr(upload, "filename", None)
    if (text is None) == (file_data is None):
        raise ValidationFailed("Provide either pasted text or one file", code="text_or_file_required")
    await _check_client(db, client_id)
    kind: str | None = None
    if file_data is not None:
        if len(file_data) > MAX_INTAKE_BYTES:
            raise ValidationFailed(
                "The file is larger than 10 MB", code="file_too_large", details={"max_mb": 10}
            )
        kind = sniff_kind(file_data)
        if kind not in PDF_DOCX:
            raise ValidationFailed(
                "Only PDF and DOCX files are supported",
                code="unsupported_file_type",
                details={"allowed": ["docx", "pdf"]},
            )
        document = _extract(file_data, kind)
    else:
        document = text or ""
    if len(document) > MAX_INTAKE_CHARS:
        raise _too_long("characters")

    result = await ai.run(
        ctx,
        db,
        "demand_intake",
        "demands_intake",
        inputs={},
        untrusted={"document": document},
        output_schema=IntakeExtraction,
        input_ref=f"demand_intake:{kind or 'text'}",
    )
    if not result.ok or not isinstance(result.data, IntakeExtraction):
        raise ai_unavailable(result.error_code)
    values = sanitize(result.data)
    extracted = sorted(values)
    title = values.pop("title", None)
    data = DemandCreate(client_id=client_id, title=title or UNTITLED, **values)
    demand = await create_demand_row(
        ctx, db, data, actor_kind="ai", ai_extracted_fields=extracted, intake_ai_action_id=result.action_id
    )
    if file_data is not None:
        await _store_file(ctx, db, demand, file_data, filename)
    else:
        await _store_text(ctx, db, demand, document)
    if result.action_id is not None:
        await ai.set_subject(db, result.action_id, "demand", demand.id)
    return await demand_out(ctx, db, demand)
