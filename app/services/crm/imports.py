# ruff: noqa: E501
"""Lead CSV import (FR-83, A3; story 2.1).

``start_import`` (request): size / row-count guards (422 ``import_too_large``), SHA-256 dedupe through the
foundation ``file_import_batches`` (an identical file returns the prior batch), the file goes to ``core.storage``
and ``crm.import_leads(tenant_id, batch_id)`` is enqueued after commit (queue ``interactive``).

``run_import`` (task): validates every row (``company_name`` required, email format, phone normalisation, known
source, revenue decimal), detects duplicates against Clients / open Leads / earlier rows (reported ``duplicate``
and skipped when ``skip_duplicates``), inserts valid rows in chunks of 500 (each chunk in a savepoint: a failing
chunk reports its rows and the others still land) with actor ``system``; audited once per batch.
"""

import csv
import dataclasses
import hashlib
import io
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, storage
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.models.clients import Client
from app.models.crm import CrmStatusHistory, Lead
from app.models.imports import FileImportBatch
from app.schemas.clients import normalize_phone
from app.schemas.crm import ImportErrorRow, LeadImportOut
from app.services import imports_service
from app.services.crm import settings as crm_settings
from app.services.crm.common import LEAD_WRITE, check_user, now, principal_of, tenant_of
from app.services.crm.duplicates import website_domain

KIND = "crm_leads"
MAX_ROWS = 5000
MAX_BYTES = 5 * 1024 * 1024
CHUNK = 500
COLUMNS = (
    "company_name",
    "website",
    "gstin",
    "industry",
    "location",
    "contact_name",
    "contact_title",
    "contact_email",
    "contact_phone",
    "source_code",
    "estimated_positions",
    "expected_annual_revenue",
    "currency",
)
TEMPLATE = (
    ",".join(COLUMNS)
    + "\n"
    + "Acme Robotics,https://acme.example.com,,Manufacturing,Pune,Asha Rao,HR Head,asha@acme.example.com,+919812345678,website,5,1200000.00,INR\n"
)


@dataclasses.dataclass(frozen=True)
class LeadImportQueued:
    """Internal after-commit signal (not a catalogue event): enqueue the import task once the batch row exists."""

    tenant_id: uuid.UUID
    batch_id: uuid.UUID


def _too_large(message: str) -> ValidationFailed:
    return ValidationFailed(
        message, code="import_too_large", details={"max_rows": MAX_ROWS, "max_bytes": MAX_BYTES}
    )


def _rows(data: bytes) -> list[tuple[int, dict[str, str]]]:
    """Non-blank data rows with their CSV row number (the header is row 1)."""
    try:
        decoded = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("The file must be UTF-8 CSV", code="import_encoding") from exc
    reader = csv.DictReader(io.StringIO(decoded))
    if not reader.fieldnames or "company_name" not in [f.strip().lower() for f in reader.fieldnames]:
        raise ValidationFailed(
            "The CSV needs a company_name column",
            code="import_columns",
            details={"required": ["company_name"]},
        )
    out: list[tuple[int, dict[str, str]]] = []
    for row in reader:
        clean = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k is not None}
        if not any(clean.values()):
            continue
        out.append((reader.line_num, clean))
        if len(out) > MAX_ROWS:
            raise _too_large(f"At most {MAX_ROWS} rows can be imported at once")
    return out


def _out(batch: FileImportBatch, *, duplicate_of: uuid.UUID | None = None) -> LeadImportOut:
    summary = batch.summary or {}
    return LeadImportOut(
        batch_id=batch.id,
        status=summary.get("state", "queued"),
        row_count=batch.row_count,
        created=int(summary.get("created", 0)),
        skipped_duplicates=int(summary.get("skipped_duplicates", 0)),
        errors=[ImportErrorRow(**e) for e in summary.get("errors", [])],
        duplicate_of=duplicate_of,
    )


async def start_import(
    ctx: RequestContext,
    db: AsyncSession,
    data: bytes,
    *,
    owner_id: uuid.UUID | None,
    default_source: str | None,
    skip_duplicates: bool,
) -> LeadImportOut:
    principal = principal_of(ctx, LEAD_WRITE)
    if len(data) > MAX_BYTES:
        raise _too_large("The file is larger than 5 MB")
    rows = _rows(data)
    digest = hashlib.sha256(data).hexdigest()
    prior = (
        await db.execute(
            select(FileImportBatch)
            .where(
                FileImportBatch.kind == KIND,
                FileImportBatch.sha256 == digest,
                FileImportBatch.status.in_(("previewed", "applied")),
            )
            .order_by(FileImportBatch.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if prior is not None:
        return _out(prior, duplicate_of=prior.id)
    if owner_id is not None and owner_id != principal.id:
        await check_user(db, owner_id, "owner_id")
    if default_source is not None:
        await crm_settings.require_source(db, default_source)
    tenant_id = tenant_of(db)
    key = storage.new_key(tenant_id)
    await storage.get_storage().put(key, data, "text/csv")
    batch = await imports_service.register_preview(
        ctx, db, KIND, None, digest, len(rows), 0,
        {
            "state": "queued",
            "storage_key": key,
            "owner_id": str(owner_id or principal.id),
            "default_source": default_source,
            "skip_duplicates": skip_duplicates,
            "uploaded_by": str(principal.id),
        },
    )  # fmt: skip
    emit(db, LeadImportQueued(tenant_id=tenant_id, batch_id=batch.id))
    row = await db.get(FileImportBatch, batch.id)
    assert row is not None
    return _out(row)


async def get_import(ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID) -> LeadImportOut:
    principal_of(ctx, LEAD_WRITE)
    batch = await db.get(FileImportBatch, batch_id)
    if batch is None or batch.kind != KIND:
        raise NotFound("Import not found")
    await db.refresh(batch)
    return _out(batch)


async def errors_csv(ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID) -> str:
    result = await get_import(ctx, db, batch_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["row", "field", "code"])
    for e in result.errors:
        writer.writerow([e.row, e.field or "", e.code])
    return buf.getvalue()


# ================================================================ task body
@dataclasses.dataclass
class _Known:
    names: set[str]
    domains: set[str]
    gstins: set[str]


async def _known(db: AsyncSession) -> _Known:
    known = _Known(set(), set(), set())
    for name, website, gstin in (
        await db.execute(select(Client.name, Client.website, Client.gstin))
    ).tuples():
        known.names.add(name.lower())
        if d := website_domain(website):
            known.domains.add(d)
        if gstin:
            known.gstins.add(gstin.upper())
    lead_rows = await db.execute(
        select(Lead.company_name, Lead.website_domain, Lead.gstin).where(
            Lead.archived_at.is_(None), Lead.status != "converted"
        )
    )
    for name, domain, gstin in lead_rows.tuples():
        known.names.add(name.lower())
        if domain:
            known.domains.add(domain)
        if gstin:
            known.gstins.add(gstin.upper())
    return known


def _validate(
    raw: dict[str, str],
    row_no: int,
    sources: set[str],
    default_source: str | None,
    errors: list[dict[str, Any]],
) -> dict[str, Any] | None:
    from app.schemas.clients import normalize_gstin, normalize_website

    def err(field: str, code: str) -> None:
        errors.append({"row": row_no, "field": field, "code": code})

    before = len(errors)
    values: dict[str, Any] = {}
    name = raw.get("company_name", "")
    if not name:
        err("company_name", "required")
    elif len(name) > 200:
        err("company_name", "too_long")
    values["company_name"] = name
    for field, limit in (("industry", 200), ("location", 300), ("contact_name", 200), ("contact_title", 200)):
        value = raw.get(field) or None
        if value and len(value) > limit:
            err(field, "too_long")
        values[field] = value
    if website := raw.get("website"):
        try:
            values["website"] = normalize_website(website)
        except ValueError:
            err("website", "invalid_website")
    if gstin := raw.get("gstin"):
        try:
            values["gstin"] = normalize_gstin(gstin)
        except ValueError:
            err("gstin", "invalid_gstin")
    if email := raw.get("contact_email"):
        try:
            values["contact_email"] = validate_email(email, check_deliverability=False).normalized.lower()
        except EmailNotValidError:
            err("contact_email", "invalid_email")
    if phone := raw.get("contact_phone"):
        try:
            values["contact_phone"] = normalize_phone(phone)
        except ValueError:
            err("contact_phone", "invalid_phone")
    source = (raw.get("source_code") or default_source or "import").lower()
    if source not in sources:
        err("source_code", "unknown_source")
    values["source_code"] = source
    if positions := raw.get("estimated_positions"):
        try:
            values["estimated_positions"] = int(positions)
            if values["estimated_positions"] < 0:
                raise ValueError
        except ValueError:
            err("estimated_positions", "invalid_number")
    if revenue := raw.get("expected_annual_revenue"):
        try:
            amount = Decimal(revenue.replace(",", ""))
            if (
                not amount.is_finite()
                or amount < 0
                or amount != amount.quantize(Decimal("0.01"))
                or amount >= Decimal("1e14")
            ):
                raise InvalidOperation
            values["expected_annual_revenue"] = amount
        except InvalidOperation:
            err("expected_annual_revenue", "invalid_decimal")
    if currency := raw.get("currency"):
        if len(currency) != 3 or not currency.isalpha():
            err("currency", "invalid_currency")
        values["currency"] = currency.upper()
    return values if len(errors) == before else None


async def _reserve_ids(db: AsyncSession, n: int) -> list[str]:
    last = (
        await db.execute(
            text(
                "INSERT INTO tenant_sequences (tenant_id, key, value) "
                "VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, 'LEAD', :n) "
                "ON CONFLICT (tenant_id, key) DO UPDATE SET value = tenant_sequences.value + :n RETURNING value"
            ),
            {"n": n},
        )
    ).scalar_one()
    first = int(last) - n + 1
    return [f"LEAD-{v:06d}" for v in range(first, int(last) + 1)]


async def run_import(ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID) -> dict[str, Any]:
    batch = (
        await db.execute(select(FileImportBatch).where(FileImportBatch.id == batch_id).with_for_update())
    ).scalar_one_or_none()
    if batch is None or batch.kind != KIND:
        return {"skipped": "missing"}
    summary = dict(batch.summary or {})
    if summary.get("state") in ("completed", "running"):
        return {"skipped": summary.get("state")}
    tenant_id = tenant_of(db)
    data = await storage.get_storage().get(summary["storage_key"])
    raw_rows = _rows(data)
    sources = await crm_settings.source_codes(db)
    known = await _known(db)
    skip_duplicates = bool(summary.get("skip_duplicates", True))
    owner_id = uuid.UUID(summary["owner_id"])
    currency_default = await crm_settings.default_currency(db)
    errors: list[dict[str, Any]] = []
    valid: list[tuple[int, dict[str, Any]]] = []
    skipped = 0
    for idx, raw in raw_rows:
        values = _validate(raw, idx, sources, summary.get("default_source"), errors)
        if values is None:
            continue
        domain = website_domain(values.get("website"))
        gst = (values.get("gstin") or "").upper() or None
        dup = (
            values["company_name"].lower() in known.names
            or (domain is not None and domain in known.domains)
            or (gst is not None and gst in known.gstins)
        )
        if dup:
            errors.append({"row": idx, "field": "company_name", "code": "duplicate"})
            if skip_duplicates:
                skipped += 1
                continue
        known.names.add(values["company_name"].lower())
        if domain:
            known.domains.add(domain)
        if gst:
            known.gstins.add(gst)
        values["website_domain"] = domain
        valid.append((idx, values))
    created = 0
    at = now()
    for start in range(0, len(valid), CHUNK):
        chunk = valid[start : start + CHUNK]
        try:
            async with db.begin_nested():
                ids = await _reserve_ids(db, len(chunk))
                lead_rows = []
                history_rows = []
                for (_, values), human_id in zip(chunk, ids, strict=True):
                    lead_id = uuid.uuid4()
                    lead_rows.append(
                        {
                            "id": lead_id,
                            "tenant_id": tenant_id,
                            "human_id": human_id,
                            "owner_id": owner_id,
                            "status": "new",
                            "currency": values.pop("currency", None) or currency_default,
                            "import_batch_id": batch.id,
                            **values,
                        }
                    )
                    history_rows.append(
                        {"id": uuid.uuid4(), "tenant_id": tenant_id, "entity_kind": "lead", "entity_id": lead_id,
                         "from_value": None, "to_value": "new", "actor_kind": "system", "actor_id": None, "at": at}
                    )  # fmt: skip
                await db.execute(pg_insert(Lead), lead_rows)
                await db.execute(pg_insert(CrmStatusHistory), history_rows)
            created += len(chunk)
        except Exception:
            errors.extend({"row": idx, "field": None, "code": "chunk_failed"} for idx, _ in chunk)
    errors.sort(key=lambda e: e["row"])
    batch.error_count = len(errors)
    batch.summary = {
        **summary,
        "state": "completed",
        "created": created,
        "skipped_duplicates": skipped,
        "errors": errors,
    }
    await db.flush()
    audit.record(
        "lead.import_applied", "file_import_batch", batch.id, session=db,
        created=created, skipped_duplicates=skipped, errors=len(errors), actor_type="system",
    )  # fmt: skip
    try:
        async with db.begin_nested():
            await imports_service.mark_applied(ctx, db, batch.id)
    except ConflictError:
        pass
    return {"created": created, "skipped_duplicates": skipped, "errors": len(errors)}


async def fail_import(ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID, reason: str) -> None:
    batch = await db.get(FileImportBatch, batch_id)
    if batch is None:
        return
    batch.summary = {**(batch.summary or {}), "state": "failed", "failure": reason[:500]}
    await db.flush()
    await imports_service.mark_failed(ctx, db, batch_id, reason=reason)
