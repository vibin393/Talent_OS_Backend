"""Rate Cards (Epic 16 A5, FR-90): versioned, effective-dated, immutable once published; templates; CSV import.

Publishing supersedes the client's current published card of the same scope (client-wide or one contract) in
one transaction: the current card ends the day before the new one starts. Published cards never overlap
(EXCLUDE gist) and are never edited (DB trigger); a change is a new version. Existing placements' rate
segments are never touched by a rate card change.
"""  # ruff: ignore[line-too-long]

import csv
import hashlib
import io
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.core.money import quantize_rate
from app.core.sequences import next_human_id
from app.events.commercials import RateCardPublished
from app.models.commercials import RATE_UNITS, Contract, RateCard, RateCardLine
from app.models.imports import FileImportBatch
from app.schemas.commercials import (
    CopyTemplateIn,
    ImportRowError,
    RateCardImportOut,
    RateCardIn,
    RateCardLineIn,
    RateCardLineOut,
    RateCardOut,
    RateCardTemplateIn,
    RateCardUpdate,
)
from app.services.commercials.common import conflict, invalid, now, require_client, tenant_of, user_id
from app.services.commercials.rate_validation import Line, normalise

ENTITY = "rate_card"
SEQUENCE_KEY = "rate_card"
MAX_IMPORT_ROWS = 2000
CSV_COLUMNS = (
    "role",
    "location",
    "experience_min_years",
    "experience_max_years",
    "min_bill_rate",
    "max_bill_rate",
    "currency",
    "rate_unit",
    "max_vendor_rate",
    "markup_pct",
)


async def get_row(db: AsyncSession, card_id: uuid.UUID, *, lock: bool = False) -> RateCard:
    stmt = select(RateCard).where(RateCard.id == card_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Rate card not found")
    return row


async def _lines(db: AsyncSession, card_id: uuid.UUID) -> list[RateCardLine]:
    return list(
        (
            await db.execute(
                select(RateCardLine)
                .where(RateCardLine.rate_card_id == card_id)
                .order_by(
                    RateCardLine.role_key,
                    RateCardLine.location_key.nulls_last(),
                    RateCardLine.experience_min_years.nulls_first(),
                )
            )
        ).scalars()
    )


async def _out(db: AsyncSession, row: RateCard, *, with_lines: bool = False) -> RateCardOut:
    lines = await _lines(db, row.id) if with_lines else None
    count = (
        len(lines)
        if lines is not None
        else (
            await db.execute(
                select(func.count()).select_from(RateCardLine).where(RateCardLine.rate_card_id == row.id)
            )
        ).scalar_one()
    )
    return RateCardOut.model_validate(row).model_copy(
        update={
            "line_count": count,
            "lines": [RateCardLineOut.model_validate(x) for x in lines] if lines is not None else None,
        }
    )


def _draft_only(row: RateCard) -> None:
    if row.status != "draft":
        raise conflict(
            "rate_card_not_draft",
            "Published rate cards are immutable; create a new version",
            status=row.status,
        )


async def _check_contract(db: AsyncSession, client_id: uuid.UUID, contract_id: uuid.UUID | None) -> None:
    if contract_id is None:
        return
    owner = (
        await db.execute(select(Contract.client_id).where(Contract.id == contract_id))
    ).scalar_one_or_none()
    if owner != client_id:
        raise invalid(
            "contract_invalid", "The contract does not belong to this client", contract_id=str(contract_id)
        )


async def _next_version(db: AsyncSession, client_id: uuid.UUID, contract_id: uuid.UUID | None) -> int:
    stmt = select(func.max(RateCard.version)).where(RateCard.client_id == client_id)
    stmt = stmt.where(
        RateCard.contract_id.is_(None) if contract_id is None else RateCard.contract_id == contract_id
    )
    return int((await db.execute(stmt)).scalar_one() or 0) + 1


async def _new_card(db: AsyncSession, **values: Any) -> RateCard:
    row = RateCard(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        human_id=await next_human_id(db, SEQUENCE_KEY, "RC"),
        status="draft",
        **values,
    )
    db.add(row)
    await db.flush()
    return row


# ================================================================ CRUD
async def list_client_cards(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID) -> list[RateCardOut]:  # ruff: ignore[unused-function-argument]
    await require_client(db, client_id)
    rows = (
        await db.execute(
            select(RateCard)
            .where(RateCard.client_id == client_id)
            .order_by(RateCard.contract_id.nulls_first(), RateCard.version.desc())
        )
    ).scalars()
    return [await _out(db, r) for r in rows]


async def create_card(ctx: RequestContext, db: AsyncSession, data: RateCardIn) -> RateCardOut:  # ruff: ignore[unused-function-argument]
    await require_client(db, data.client_id)
    await _check_contract(db, data.client_id, data.contract_id)
    row = await _new_card(
        db,
        client_id=data.client_id,
        contract_id=data.contract_id,
        is_template=False,
        name=data.name,
        version=await _next_version(db, data.client_id, data.contract_id),
        effective_from=data.effective_from,
    )
    return await _out(db, row, with_lines=True)


async def get_card(ctx: RequestContext, db: AsyncSession, card_id: uuid.UUID) -> RateCardOut:  # ruff: ignore[unused-function-argument]
    return await _out(db, await get_row(db, card_id), with_lines=True)


async def update_card(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    card_id: uuid.UUID,
    data: RateCardUpdate,
) -> RateCardOut:
    row = await get_row(db, card_id, lock=True)
    _draft_only(row)
    changes = data.model_dump(exclude_unset=True)
    if "contract_id" in changes:
        if row.is_template or row.client_id is None:
            raise invalid("contract_invalid", "Templates are not contract-scoped")
        await _check_contract(db, row.client_id, changes["contract_id"])
    for key, value in changes.items():
        if value is None and key in ("name", "effective_from"):
            continue
        setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    return await _out(db, row, with_lines=True)


def _line_values(row: RateCard, data: RateCardLineIn) -> dict[str, Any]:
    if data.max_bill_rate < data.min_bill_rate:
        raise invalid("bill_range_invalid", "The maximum bill rate is below the minimum", role=data.role)
    if (
        data.experience_min_years is not None
        and data.experience_max_years is not None
        and data.experience_min_years > data.experience_max_years
    ):
        raise invalid("experience_range_invalid", "The experience range is inverted", role=data.role)
    values = data.model_dump()
    for key in ("min_bill_rate", "max_bill_rate", "max_vendor_rate"):
        if values[key] is not None:
            values[key] = quantize_rate(values[key])
    return {
        **values,
        "role_key": normalise(data.role),
        "location": (data.location or None),
        "location_key": normalise(data.location),
        "client_id": row.client_id,
        "rate_card_id": row.id,
        "tenant_id": row.tenant_id,
    }


async def _replace_lines(db: AsyncSession, row: RateCard, lines: list[RateCardLineIn]) -> None:
    for existing in await _lines(db, row.id):
        await db.delete(existing)
    await db.flush()
    for line in lines:
        db.add(RateCardLine(id=uuid.uuid4(), **_line_values(row, line)))
    await db.flush()


async def put_lines(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    card_id: uuid.UUID,
    lines: list[RateCardLineIn],
) -> RateCardOut:
    row = await get_row(db, card_id, lock=True)
    _draft_only(row)
    await _replace_lines(db, row, lines)
    await db.refresh(row)
    return await _out(db, row, with_lines=True)


# ================================================================ publish / archive / versions
def _scope(stmt: Any, row: RateCard) -> Any:
    stmt = stmt.where(RateCard.client_id == row.client_id)
    return stmt.where(
        RateCard.contract_id.is_(None) if row.contract_id is None else RateCard.contract_id == row.contract_id
    )


async def current_published(db: AsyncSession, row: RateCard, *, lock: bool = False) -> RateCard | None:
    stmt = _scope(select(RateCard).where(RateCard.status == "published", RateCard.id != row.id), row)
    stmt = stmt.order_by(RateCard.effective_from.desc()).limit(1)
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def publish(ctx: RequestContext, db: AsyncSession, card_id: uuid.UUID) -> RateCardOut:
    row = await get_row(db, card_id, lock=True)
    _draft_only(row)
    if row.is_template or row.client_id is None:
        row.status = "published"
        row.published_at = now()
        row.published_by = user_id(ctx)
        await db.flush()
        await db.refresh(row)
        return await _out(db, row, with_lines=True)
    if row.effective_from is None:
        raise invalid("effective_from_required", "Set the effective date before publishing")
    if not await _lines(db, row.id):
        raise invalid("rate_card_empty", "Add at least one line before publishing")
    await _check_contract(db, row.client_id, row.contract_id)
    current = await current_published(db, row, lock=True)
    if current is not None:
        assert current.effective_from is not None
        if row.effective_from <= current.effective_from:
            raise conflict(
                "rate_card_overlap",
                "The new version must start after the current version's start date",
                current_rate_card_id=str(current.id),
                current_effective_from=current.effective_from.isoformat(),
            )
        current.effective_to = row.effective_from - timedelta(days=1)
        current.status = "superseded"
        await db.flush()
    else:
        # Later superseded versions never overlap either (EXCLUDE): refuse a start inside an older range.
        overlapping = (
            await db.execute(
                _scope(
                    select(RateCard.id).where(
                        RateCard.status == "superseded", RateCard.effective_to >= row.effective_from
                    ),
                    row,
                )
            )
        ).first()
        if overlapping is not None:
            raise conflict("rate_card_overlap", "The effective date overlaps an earlier version")
    row.status = "published"
    row.published_at = now()
    row.published_by = user_id(ctx)
    await db.flush()
    emit(
        db,
        RateCardPublished(
            tenant_id=row.tenant_id,
            rate_card_id=row.id,
            client_id=row.client_id,
            contract_id=row.contract_id,
            effective_from=row.effective_from,
        ),
    )
    from app.services.commercials.common import timeline

    await timeline(
        ctx,
        db,
        "client",
        row.client_id,
        f"Rate card {row.human_id} v{row.version} published",
        {"rate_card_id": str(row.id)},
    )
    await db.refresh(row)
    return await _out(db, row, with_lines=True)


async def archive(ctx: RequestContext, db: AsyncSession, card_id: uuid.UUID) -> RateCardOut:  # ruff: ignore[unused-function-argument]
    row = await get_row(db, card_id, lock=True)
    if row.status == "archived":
        return await _out(db, row)
    if row.status == "draft":
        row.status = "archived"
        row.archived_at = now()
    else:
        row.status = "archived"
        row.archived_at = now()
    await db.flush()
    await db.refresh(row)
    return await _out(db, row)


async def new_version(ctx: RequestContext, db: AsyncSession, card_id: uuid.UUID) -> RateCardOut:  # ruff: ignore[unused-function-argument]
    source = await get_row(db, card_id)
    if source.is_template or source.client_id is None:
        raise invalid("rate_card_template", "Copy a template to a client instead")
    row = await _new_card(
        db,
        client_id=source.client_id,
        contract_id=source.contract_id,
        is_template=False,
        name=source.name,
        version=await _next_version(db, source.client_id, source.contract_id),
        effective_from=None,
        source_template_id=source.source_template_id,
    )
    await _copy_lines(db, source, row)
    return await _out(db, row, with_lines=True)


async def _copy_lines(db: AsyncSession, source: RateCard, target: RateCard) -> None:
    for line in await _lines(db, source.id):
        db.add(
            RateCardLine(
                id=uuid.uuid4(),
                tenant_id=target.tenant_id,
                client_id=target.client_id,
                rate_card_id=target.id,
                role=line.role,
                role_key=line.role_key,
                location=line.location,
                location_key=line.location_key,
                experience_min_years=line.experience_min_years,
                experience_max_years=line.experience_max_years,
                min_bill_rate=line.min_bill_rate,
                max_bill_rate=line.max_bill_rate,
                currency=line.currency,
                rate_unit=line.rate_unit,
                max_vendor_rate=line.max_vendor_rate,
                markup_pct=line.markup_pct,
            )
        )
    await db.flush()


async def effective_card(
    db: AsyncSession, client_id: uuid.UUID, on_date: date, contract_id: uuid.UUID | None = None
) -> RateCard | None:
    """Contract-scoped published / superseded card effective on ``on_date`` if ``contract_id`` is given (falls
    back to the client-wide card), else the client-wide one."""

    async def find(contract: uuid.UUID | None) -> RateCard | None:
        stmt = select(RateCard).where(
            RateCard.client_id == client_id,
            RateCard.status.in_(("published", "superseded")),
            RateCard.effective_from <= on_date,
            (RateCard.effective_to.is_(None)) | (RateCard.effective_to >= on_date),
        )
        stmt = stmt.where(
            RateCard.contract_id.is_(None) if contract is None else RateCard.contract_id == contract
        )
        return (await db.execute(stmt.order_by(RateCard.effective_from.desc()).limit(1))).scalar_one_or_none()

    if contract_id is not None:
        found = await find(contract_id)
        if found is not None:
            return found
    return await find(None)


async def effective_lines(db: AsyncSession, card: RateCard) -> list[Line]:
    return [
        Line(
            id=x.id,
            role_key=x.role_key,
            location_key=x.location_key,
            experience_min_years=x.experience_min_years,
            experience_max_years=x.experience_max_years,
            min_bill_rate=x.min_bill_rate,
            max_bill_rate=x.max_bill_rate,
            currency=x.currency,
            rate_unit=x.rate_unit,
            max_vendor_rate=x.max_vendor_rate,
        )
        for x in await _lines(db, card.id)
    ]


async def get_effective(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    on_date: date,
    contract_id: uuid.UUID | None,
) -> RateCardOut | None:
    await require_client(db, client_id)
    card = await effective_card(db, client_id, on_date, contract_id)
    return None if card is None else await _out(db, card, with_lines=True)


# ================================================================ templates
async def list_templates(ctx: RequestContext, db: AsyncSession) -> list[RateCardOut]:  # ruff: ignore[unused-function-argument]
    rows = (
        await db.execute(
            select(RateCard)
            .where(RateCard.is_template.is_(True), RateCard.status != "archived")
            .order_by(RateCard.name)
        )
    ).scalars()
    return [await _out(db, r) for r in rows]


async def create_template(ctx: RequestContext, db: AsyncSession, data: RateCardTemplateIn) -> RateCardOut:  # ruff: ignore[unused-function-argument]
    row = await _new_card(db, client_id=None, contract_id=None, is_template=True, name=data.name, version=1)
    return await _out(db, row, with_lines=True)


async def copy_rate_card_template(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    template_id: uuid.UUID,
    client_id: uuid.UUID,
    contract_id: uuid.UUID | None,
    effective_from: date,
) -> uuid.UUID:
    """A **draft** client card copied from a template (used by the CRM Won conversion)."""  # ruff: ignore[non-imperative-mood]
    template = await get_row(db, template_id)
    if not template.is_template:
        raise invalid("rate_card_not_template", "This rate card is not a template")
    await require_client(db, client_id)
    await _check_contract(db, client_id, contract_id)
    row = await _new_card(
        db,
        client_id=client_id,
        contract_id=contract_id,
        is_template=False,
        name=template.name,
        version=await _next_version(db, client_id, contract_id),
        effective_from=effective_from,
        source_template_id=template.id,
    )
    await _copy_lines(db, template, row)
    return row.id


async def copy_template(
    ctx: RequestContext, db: AsyncSession, template_id: uuid.UUID, data: CopyTemplateIn
) -> RateCardOut:
    card_id = await copy_rate_card_template(
        ctx, db, template_id, data.client_id, data.contract_id, data.effective_from
    )
    return await _out(db, await get_row(db, card_id), with_lines=True)


# ================================================================ CSV import
def _dec(
    raw: str, field: str, row_no: int, errors: list[ImportRowError], *, required: bool
) -> Decimal | None:
    value = (raw or "").strip().replace(",", "")
    if not value:
        if required:
            errors.append(
                ImportRowError(row=row_no, field=field, code="required", message=f"{field} is required")
            )
        return None
    try:
        out = Decimal(value)
    except InvalidOperation:
        errors.append(
            ImportRowError(row=row_no, field=field, code="invalid_number", message=f"{field} is not a number")
        )
        return None
    if out < 0:
        errors.append(
            ImportRowError(row=row_no, field=field, code="negative", message=f"{field} must not be negative")
        )
        return None
    return out


def parse_csv(data: bytes) -> tuple[list[RateCardLineIn], list[ImportRowError], int]:
    """Rows -> lines + row errors (row numbers count the header as row 1)."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return (
            [],
            [ImportRowError(row=0, field=None, code="encoding", message="The file must be UTF-8 CSV")],
            0,
        )
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in ("role", "min_bill_rate", "max_bill_rate") if c not in header]
    if missing:
        return (
            [],
            [
                ImportRowError(
                    row=1,
                    field=None,
                    code="missing_columns",
                    message=f"Missing columns: {', '.join(missing)}",
                )
            ],
            0,
        )
    reader.fieldnames = header
    lines: list[RateCardLineIn] = []
    errors: list[ImportRowError] = []
    total = 0
    for i, raw in enumerate(reader, start=2):
        total += 1
        if total > MAX_IMPORT_ROWS:
            errors.append(
                ImportRowError(
                    row=i, field=None, code="too_many_rows", message=f"At most {MAX_IMPORT_ROWS} rows"
                )
            )
            break
        row_errors: list[ImportRowError] = []
        role = (raw.get("role") or "").strip()
        if not role:
            row_errors.append(
                ImportRowError(row=i, field="role", code="required", message="role is required")
            )
        values = {
            "experience_min_years": _dec(
                raw.get("experience_min_years") or "", "experience_min_years", i, row_errors, required=False
            ),
            "experience_max_years": _dec(
                raw.get("experience_max_years") or "", "experience_max_years", i, row_errors, required=False
            ),
            "min_bill_rate": _dec(
                raw.get("min_bill_rate") or "", "min_bill_rate", i, row_errors, required=True
            ),
            "max_bill_rate": _dec(
                raw.get("max_bill_rate") or "", "max_bill_rate", i, row_errors, required=True
            ),
            "max_vendor_rate": _dec(
                raw.get("max_vendor_rate") or "", "max_vendor_rate", i, row_errors, required=False
            ),
            "markup_pct": _dec(raw.get("markup_pct") or "", "markup_pct", i, row_errors, required=False),
        }
        currency = (raw.get("currency") or "INR").strip().upper()
        unit = (raw.get("rate_unit") or "hour").strip().lower()
        if len(currency) != 3 or not currency.isalpha():
            row_errors.append(
                ImportRowError(
                    row=i,
                    field="currency",
                    code="invalid_currency",
                    message="currency must be a 3-letter code",
                )
            )
        if unit not in RATE_UNITS:
            row_errors.append(
                ImportRowError(
                    row=i,
                    field="rate_unit",
                    code="invalid_rate_unit",
                    message=f"rate_unit must be one of {', '.join(RATE_UNITS)}",
                )
            )
        mn, mx = values["min_bill_rate"], values["max_bill_rate"]
        if mn is not None and mx is not None and mn > mx:
            row_errors.append(
                ImportRowError(
                    row=i,
                    field="max_bill_rate",
                    code="bill_range_invalid",
                    message="max_bill_rate is below min_bill_rate",
                )
            )
        emin, emax = values["experience_min_years"], values["experience_max_years"]
        if emin is not None and emax is not None and emin > emax:
            row_errors.append(
                ImportRowError(
                    row=i,
                    field="experience_max_years",
                    code="experience_range_invalid",
                    message="experience range is inverted",
                )
            )
        if row_errors:
            errors.extend(row_errors)
            continue
        lines.append(
            RateCardLineIn(
                role=role,
                location=(raw.get("location") or "").strip() or None,
                currency=currency,
                rate_unit=unit,
                **values,
            )
        )
    return lines, errors, total


async def import_csv(
    ctx: RequestContext, db: AsyncSession, card_id: uuid.UUID, data: bytes, *, skip_invalid: bool = False
) -> RateCardImportOut:
    row = await get_row(db, card_id, lock=True)
    _draft_only(row)
    sha = hashlib.sha256(data).hexdigest()
    kind = f"rate_card:{row.id}"[:40]
    prior = (
        await db.execute(
            select(FileImportBatch).where(
                FileImportBatch.kind == kind,
                FileImportBatch.sha256 == sha,
                FileImportBatch.status == "applied",
            )
        )
    ).scalar_one_or_none()
    if prior is not None:
        summary = prior.summary or {}
        return RateCardImportOut(
            applied=int(summary.get("applied", 0)),
            errors=[ImportRowError(**e) for e in summary.get("errors", [])],
            duplicate=True,
        )
    lines, errors, _total = parse_csv(data)
    if errors and not skip_invalid:
        return RateCardImportOut(applied=0, errors=errors)
    current = await _lines(db, row.id)
    keep = [
        RateCardLineIn(
            role=x.role,
            location=x.location,
            experience_min_years=x.experience_min_years,
            experience_max_years=x.experience_max_years,
            min_bill_rate=x.min_bill_rate,
            max_bill_rate=x.max_bill_rate,
            currency=x.currency,
            rate_unit=x.rate_unit,
            max_vendor_rate=x.max_vendor_rate,
            markup_pct=x.markup_pct,
        )
        for x in current
    ]
    await _replace_lines(db, row, keep + lines)
    result = RateCardImportOut(applied=len(lines), errors=errors)
    db.add(
        FileImportBatch(
            id=uuid.uuid4(),
            tenant_id=tenant_of(db),
            kind=kind,
            sha256=sha,
            status="applied",
            row_count=len(lines) + len(errors),
            error_count=len(errors),
            summary=result.model_dump(mode="json"),
            applied_at=now(),
            applied_by=user_id(ctx),
        )
    )
    await db.flush()
    return result
