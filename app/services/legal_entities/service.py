"""Legal Entities, GST registrations, LUTs, bank accounts and numbering series (Epic 13, AD-17, FR-76).

* Entities are tenant data; every tenant has at least one (backfilled / provisioned). Deactivated entities
  refuse new documents (``assert_accepts_new_documents``); entities referenced by placements, invoices, debit
  notes, contracts or purchase orders -- or the tenant's last active entity -- are never deleted.
* GST registrations are validated once (``gstin.validate_gstin``); one principal registration per entity.
* Bank account numbers are sealed (``core.encrypted``) and only ``last4`` is ever returned; documents read the
  decrypted company account through ``bank_account_for_documents`` (system read, audited).
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import column, func, select, table, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.encrypted import open_sealed, set_identity_field
from app.core.errors import ConflictError, NotFound, ValidationFailed, field_errors_failed
from app.core.events import emit
from app.events.legal_entities import GstRegistrationChanged, LegalEntityCreated, LegalEntityDeactivated
from app.models.legal_entities import (
    DocumentSeries,
    GstRegistration,
    GstRegistrationLut,
    LegalEntity,
    LegalEntityBankAccount,
)
from app.models.reference import Currency, GstState
from app.schemas.legal_entities import (
    BankAccountIn,
    BankAccountOut,
    BankAccountUnmaskedOut,
    BankAccountUpdate,
    CurrencyOut,
    DocumentBankAccount,
    DocumentSeriesIn,
    DocumentSeriesOut,
    DocumentSeriesUpdate,
    GstRegistrationIn,
    GstRegistrationOut,
    GstRegistrationUpdate,
    GstStateOut,
    LegalEntityIn,
    LegalEntityOut,
    LegalEntityReadinessOut,
    LegalEntityUpdate,
    LutIn,
    LutOut,
    LutPutIn,
)
from app.services.legal_entities import numbering, periods
from app.services.legal_entities.gstin import validate_gstin

# Tables (owned by other domains) that pin a Legal Entity.
_REFERENCING_TABLES = (
    "placements",
    "invoices",
    "credit_notes",
    "debit_notes",
    "contracts",
    "purchase_orders",
    "pay_periods",  # Epic 28 (Epic 14 FR-76: entities with pay periods are never deleted)
)
# Documents whose existence locks the entity's tax identity (timezone, state, country, currency).
_DOCUMENT_TABLES = ("invoices", "credit_notes", "debit_notes")
_LOCKED_FIELDS = ("timezone", "state_code", "country", "default_currency")
_business_units = table("business_units", column("id"), column("default_legal_entity_id"))


def _tenant(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "legal entities need a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def _entity_name_taken() -> ConflictError:
    return ConflictError("Another legal entity already has this name", code="legal_entity_name_taken")


# ================================================================ reference checks
async def _check_reference(
    db: AsyncSession, *, state_code: str | None, currency: str | None, timezone: str | None
) -> None:
    if (
        state_code is not None
        and (await db.execute(select(GstState.code).where(GstState.code == state_code))).first() is None
    ):
        raise ValidationFailed(
            "Unknown GST state code", code="unknown_state_code", details={"state_code": state_code}
        )
    if currency is not None:
        row = (await db.execute(select(Currency).where(Currency.code == currency))).scalar_one_or_none()
        if row is None or not row.document_allowed:
            raise ValidationFailed(
                "This currency cannot be used on documents",
                code="currency_not_supported",
                details={"currency": currency},
            )
    if timezone is not None:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationFailed(
                "Unknown timezone", code="invalid_timezone", details={"timezone": timezone}
            ) from exc


# ================================================================ entities
async def _get(db: AsyncSession, legal_entity_id: uuid.UUID, *, lock: bool = False) -> LegalEntity:
    stmt = select(LegalEntity).where(LegalEntity.id == legal_entity_id)
    if lock:
        stmt = stmt.with_for_update()
    entity = (await db.execute(stmt)).scalar_one_or_none()
    if entity is None:
        raise NotFound("Legal entity not found")
    return entity


async def list_entities(ctx: RequestContext, db: AsyncSession) -> list[LegalEntityOut]:  # ruff: ignore[unused-function-argument]
    rows = (await db.execute(select(LegalEntity).order_by(LegalEntity.name))).scalars()
    return [LegalEntityOut.model_validate(r) for r in rows]


async def get_entity(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID) -> LegalEntityOut:  # ruff: ignore[unused-function-argument]
    return LegalEntityOut.model_validate(await _get(db, legal_entity_id))


async def create_entity(ctx: RequestContext, db: AsyncSession, data: LegalEntityIn) -> LegalEntityOut:  # ruff: ignore[unused-function-argument]
    await _check_reference(
        db, state_code=data.state_code, currency=data.default_currency, timezone=data.timezone
    )
    entity = LegalEntity(
        id=uuid.uuid4(), tenant_id=_tenant(db), status="active", is_backfilled=False, **data.model_dump()
    )
    try:
        async with db.begin_nested():
            db.add(entity)
            await db.flush()
    except IntegrityError as exc:
        raise _entity_name_taken() from exc
    await periods.ensure_window(db, entity.id, entity.timezone)
    emit(db, LegalEntityCreated(tenant_id=entity.tenant_id, legal_entity_id=entity.id))
    await db.refresh(entity)
    return LegalEntityOut.model_validate(entity)


async def update_entity(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    data: LegalEntityUpdate,
) -> LegalEntityOut:
    entity = await _get(db, legal_entity_id, lock=True)
    changes = data.model_dump(exclude_unset=True)
    await _check_field_locks(db, entity, changes)
    if "pan" in changes and changes["pan"] != entity.pan:
        await _check_pan_against_registrations(db, entity.id, changes["pan"])
    await _check_reference(
        db,
        state_code=changes.get("state_code"),
        currency=changes.get("default_currency"),
        timezone=changes.get("timezone"),
    )
    for key, value in changes.items():
        if (
            key in {"name", "legal_name", "country", "default_currency", "timezone", "address"}
            and value is None
        ):
            continue
        setattr(entity, key, value)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise _entity_name_taken() from exc
    await db.refresh(entity)
    return LegalEntityOut.model_validate(entity)


async def _active_count(db: AsyncSession) -> int:
    return int((await db.execute(select(func.count()).where(LegalEntity.status == "active"))).scalar_one())


async def deactivate(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    reason: str | None = None,
) -> LegalEntityOut:
    entity = await _get(db, legal_entity_id, lock=True)
    if entity.status == "inactive":
        return LegalEntityOut.model_validate(entity)
    if await _active_count(db) <= 1:
        raise ConflictError(
            "The last active legal entity cannot be deactivated", code="legal_entity_last_active"
        )
    entity.status = "inactive"
    # Epic 14 (FR-76, AD-5): the reason is kept on the entity and audited explicitly.
    entity.deactivation_reason = reason
    entity.deactivated_at = datetime.now(UTC)
    await db.flush()
    audit.record("legal_entity.deactivated", "legal_entity", entity.id, session=db, reason=reason)
    emit(db, LegalEntityDeactivated(tenant_id=entity.tenant_id, legal_entity_id=entity.id))
    await db.refresh(entity)
    return LegalEntityOut.model_validate(entity)


async def activate(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID) -> LegalEntityOut:  # ruff: ignore[unused-function-argument]
    entity = await _get(db, legal_entity_id, lock=True)
    if entity.status != "active":
        entity.status = "active"
        entity.deactivation_reason = None
        entity.deactivated_at = None
        await db.flush()
        audit.record("legal_entity.reactivated", "legal_entity", entity.id, session=db)
        await periods.ensure_window(db, entity.id, entity.timezone)
        await db.refresh(entity)
    return LegalEntityOut.model_validate(entity)


async def _in_use(db: AsyncSession, legal_entity_id: uuid.UUID) -> list[str]:
    used: list[str] = []
    for name in _REFERENCING_TABLES:
        if not await _table_exists(db, name):
            continue
        t = table(name, column("legal_entity_id"))
        if (
            await db.execute(select(1).select_from(t).where(t.c.legal_entity_id == legal_entity_id).limit(1))
        ).first():
            used.append(name)
    return used


async def delete_entity(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID) -> None:  # ruff: ignore[unused-function-argument]
    entity = await _get(db, legal_entity_id, lock=True)
    used = await _in_use(db, legal_entity_id)
    if await _closed_periods(db, legal_entity_id):
        used.append("accounting_periods")
    if used:
        raise ConflictError(
            "This legal entity is in use", code="legal_entity_in_use", details={"tables": used}
        )
    if entity.status == "active" and await _active_count(db) <= 1:
        raise ConflictError(
            "The last active legal entity cannot be deleted",
            code="legal_entity_in_use",
            details={"tables": []},
        )
    await db.execute(
        update(_business_units)
        .where(_business_units.c.default_legal_entity_id == legal_entity_id)
        .values(default_legal_entity_id=None)
    )
    # Series, periods and registrations go with the entity (LUTs / bank accounts cascade).
    await db.execute(
        DocumentSeries.__table__.delete().where(DocumentSeries.legal_entity_id == legal_entity_id)  # type: ignore[attr-defined]
    )
    from app.models.legal_entities import AccountingPeriod

    await db.execute(
        AccountingPeriod.__table__.delete().where(AccountingPeriod.legal_entity_id == legal_entity_id)  # type: ignore[attr-defined]
    )
    await db.execute(
        GstRegistration.__table__.delete().where(GstRegistration.legal_entity_id == legal_entity_id)  # type: ignore[attr-defined]
    )
    try:
        async with db.begin_nested():
            await db.delete(entity)
            await db.flush()
    except IntegrityError as exc:  # e.g. a connector configuration still points at it
        raise ConflictError(
            "This legal entity is in use", code="legal_entity_in_use", details={"tables": ["other"]}
        ) from exc


async def provision_default_entity(db: AsyncSession, tenant: Any) -> LegalEntityOut:
    """Create the tenant's default Legal Entity (name / currency / timezone of the tenant), point its business
    units at it and open its periods (current month through + 2). Idempotent per tenant."""
    existing = (
        await db.execute(select(LegalEntity).order_by(LegalEntity.created_at).limit(1))
    ).scalar_one_or_none()
    if existing is not None:
        return LegalEntityOut.model_validate(existing)
    currency = str(getattr(tenant, "currency", "INR") or "INR").upper()
    row = (await db.execute(select(Currency).where(Currency.code == currency))).scalar_one_or_none()
    if row is None or not row.document_allowed:
        currency = "INR"
    entity = LegalEntity(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name=str(tenant.name)[:200],
        legal_name=str(tenant.name)[:300],
        default_currency=currency,
        timezone=str(getattr(tenant, "timezone", None) or "Asia/Kolkata"),
        country="IN",
        status="active",
        is_backfilled=True,
    )
    db.add(entity)
    await db.flush()
    await db.execute(
        update(_business_units)
        .where(_business_units.c.default_legal_entity_id.is_(None))
        .values(default_legal_entity_id=entity.id)
    )
    await periods.ensure_window(db, entity.id, entity.timezone)
    emit(db, LegalEntityCreated(tenant_id=entity.tenant_id, legal_entity_id=entity.id))
    await db.refresh(entity)
    return LegalEntityOut.model_validate(entity)


async def _principal_registration(db: AsyncSession, legal_entity_id: uuid.UUID) -> GstRegistration | None:
    return (
        await db.execute(
            select(GstRegistration).where(
                GstRegistration.legal_entity_id == legal_entity_id,
                GstRegistration.is_principal.is_(True),
                GstRegistration.status == "active",
            )
        )
    ).scalar_one_or_none()


async def assert_accepts_new_documents(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    *,
    require_complete: bool = False,
) -> None:
    """409 ``legal_entity_inactive``; with ``require_complete`` 422 ``legal_entity_incomplete`` when the PAN,
    the state or (country IN) a principal GST registration is missing. Epic 14 extends the rule."""
    entity = await _get(db, legal_entity_id)
    if entity.status != "active":
        raise ConflictError("This legal entity is inactive", code="legal_entity_inactive")
    if not require_complete:
        return
    missing = await readiness_missing(db, entity)
    if missing:
        raise ValidationFailed(
            "Complete the legal entity before issuing documents",
            code="legal_entity_incomplete",
            details={"missing": missing},
        )


async def default_registration_for(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    state_code: str | None,
) -> uuid.UUID | None:
    if state_code:
        row = (
            await db.execute(
                select(GstRegistration.id)
                .where(
                    GstRegistration.legal_entity_id == legal_entity_id,
                    GstRegistration.state_code == state_code,
                    GstRegistration.status == "active",
                )
                .order_by(GstRegistration.is_principal.desc(), GstRegistration.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            return row
    principal = await _principal_registration(db, legal_entity_id)
    return principal.id if principal is not None else None


# ================================================================ GST registrations / LUTs
async def list_registrations(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
) -> list[GstRegistrationOut]:
    await _get(db, legal_entity_id)
    rows = (
        await db.execute(
            select(GstRegistration)
            .where(GstRegistration.legal_entity_id == legal_entity_id)
            .order_by(GstRegistration.is_principal.desc(), GstRegistration.gstin)
        )
    ).scalars()
    return [GstRegistrationOut.model_validate(r) for r in rows]


async def _clear_principal(db: AsyncSession, legal_entity_id: uuid.UUID, keep: uuid.UUID | None) -> None:
    for row in (
        await db.execute(
            select(GstRegistration).where(
                GstRegistration.legal_entity_id == legal_entity_id, GstRegistration.is_principal.is_(True)
            )
        )
    ).scalars():
        if row.id != keep:
            row.is_principal = False
    await db.flush()


async def create_registration(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    data: GstRegistrationIn,
) -> GstRegistrationOut:
    entity = await _get(db, legal_entity_id, lock=True)
    await _check_reference(db, state_code=data.state_code, currency=None, timezone=None)
    errors = validate_gstin(data.gstin, state_code=data.state_code, pan=entity.pan)
    if errors:
        raise field_errors_failed(errors)
    await _check_one_active_per_state(db, legal_entity_id, data.state_code, None)
    existing_principal = await _principal_registration(db, legal_entity_id)
    make_principal = data.is_principal or existing_principal is None
    if make_principal:
        await _clear_principal(db, legal_entity_id, None)
    row = GstRegistration(
        id=uuid.uuid4(),
        tenant_id=entity.tenant_id,
        legal_entity_id=legal_entity_id,
        gstin=data.gstin,
        state_code=data.state_code,
        address=data.address,
        is_principal=make_principal,
        einvoice_applicable=data.einvoice_applicable,
        irp_reporting_limit=data.irp_reporting_limit,
        status="active",
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError("This GSTIN is already registered", code="gstin_taken") from exc
    emit(
        db,
        GstRegistrationChanged(
            tenant_id=row.tenant_id, gst_registration_id=row.id, legal_entity_id=legal_entity_id
        ),
    )
    await db.refresh(row)
    return GstRegistrationOut.model_validate(row)


async def _registration(
    db: AsyncSession, gst_registration_id: uuid.UUID, *, lock: bool = False
) -> GstRegistration:
    stmt = select(GstRegistration).where(GstRegistration.id == gst_registration_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("GST registration not found")
    return row


async def update_registration(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    gst_registration_id: uuid.UUID,
    data: GstRegistrationUpdate,
) -> GstRegistrationOut:
    row = await _registration(db, gst_registration_id, lock=True)
    changes = data.model_dump(exclude_unset=True)
    if changes.get("status") == "active" and row.status != "active":
        await _check_one_active_per_state(db, row.legal_entity_id, row.state_code, row.id)
    if changes.get("is_principal"):
        await _clear_principal(db, row.legal_entity_id, row.id)
    for key, value in changes.items():
        if value is not None:
            setattr(row, key, value)
    await db.flush()
    emit(
        db,
        GstRegistrationChanged(
            tenant_id=row.tenant_id, gst_registration_id=row.id, legal_entity_id=row.legal_entity_id
        ),
    )
    await db.refresh(row)
    return GstRegistrationOut.model_validate(row)


async def get_gst_registration(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    gst_registration_id: uuid.UUID,
) -> GstRegistrationOut:
    return GstRegistrationOut.model_validate(await _registration(db, gst_registration_id))


async def add_lut(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    gst_registration_id: uuid.UUID,
    data: LutIn,
) -> LutOut:
    registration = await _registration(db, gst_registration_id)
    if data.valid_to < data.valid_from:
        raise ValidationFailed("valid_to must not be before valid_from", code="lut_validity")
    row = GstRegistrationLut(
        id=uuid.uuid4(),
        tenant_id=registration.tenant_id,
        gst_registration_id=registration.id,
        fiscal_year=data.fiscal_year,
        lut_number=data.lut_number,
        valid_from=data.valid_from,
        valid_to=data.valid_to,
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError("A LUT for this financial year already exists", code="lut_exists") from exc
    return LutOut.model_validate(row)


async def get_lut(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    gst_registration_id: uuid.UUID,
    fiscal_year: str,
) -> LutOut | None:
    row = (
        await db.execute(
            select(GstRegistrationLut).where(
                GstRegistrationLut.gst_registration_id == gst_registration_id,
                GstRegistrationLut.fiscal_year == fiscal_year,
            )
        )
    ).scalar_one_or_none()
    return LutOut.model_validate(row) if row is not None else None


# ================================================================ bank accounts
def _bank_out(row: LegalEntityBankAccount) -> BankAccountOut:
    return BankAccountOut(
        id=row.id,
        legal_entity_id=row.legal_entity_id,
        label=row.label,
        bank_name=row.bank_name,
        account_number_last4=row.account_number_last4,
        ifsc=row.ifsc,
        swift=row.swift,
        currency=row.currency,
        is_default=row.is_default,
        created_at=row.created_at,
        branch=row.branch,
        account_name=row.account_name,
        archived_at=row.archived_at,
    )


async def list_bank_accounts(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
) -> list[BankAccountOut]:
    await _get(db, legal_entity_id)
    rows = (
        await db.execute(
            select(LegalEntityBankAccount)
            .where(LegalEntityBankAccount.legal_entity_id == legal_entity_id)
            .order_by(LegalEntityBankAccount.is_default.desc(), LegalEntityBankAccount.label)
        )
    ).scalars()
    return [_bank_out(r) for r in rows]


async def create_bank_account(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    data: BankAccountIn,
) -> BankAccountOut:
    entity = await _get(db, legal_entity_id, lock=True)
    if data.currency is not None:
        await _check_reference(db, state_code=None, currency=data.currency, timezone=None)
    # Epic 14: one default per (entity, currency) among non-archived accounts.
    has_default = await _default_account_id(db, legal_entity_id, data.currency) is not None
    make_default = data.is_default or not has_default
    if make_default and has_default:
        await _clear_default(db, legal_entity_id, data.currency, keep=None)
    row = LegalEntityBankAccount(
        id=uuid.uuid4(),
        tenant_id=entity.tenant_id,
        legal_entity_id=legal_entity_id,
        label=data.label,
        bank_name=data.bank_name,
        ifsc=data.ifsc,
        swift=data.swift,
        currency=data.currency,
        is_default=make_default,
        branch=data.branch,
        account_name=data.account_name,
    )
    set_identity_field(row, "account_number", data.account_number, entity.tenant_id)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return _bank_out(row)


async def bank_account_for_documents(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    currency: str,
) -> DocumentBankAccount:
    """The company account printed on invoices / statements: the default account for ``currency`` (or for
    any currency), else the default account. System read, audited; never returned by an API."""
    rows = list(
        (
            await db.execute(
                select(LegalEntityBankAccount).where(
                    LegalEntityBankAccount.legal_entity_id == legal_entity_id,
                    LegalEntityBankAccount.archived_at.is_(None),
                )
            )
        ).scalars()
    )
    ccy = currency.upper()

    def rank(r: LegalEntityBankAccount) -> tuple[int, int]:
        currency_rank = 0 if r.currency == ccy else (1 if r.currency is None else 9)
        return currency_rank, 0 if r.is_default else 1

    candidates = sorted((r for r in rows if r.currency in (ccy, None)), key=rank)
    if not candidates:
        raise ConflictError(
            "No bank account is set up for this legal entity and currency",
            code="bank_account_missing",
            details={"legal_entity_id": str(legal_entity_id), "currency": ccy},
        )
    chosen = candidates[0]
    audit.record("bank_account.read_for_document", "legal_entity_bank_account", chosen.id, session=db)
    return DocumentBankAccount(
        bank_account_id=chosen.id,
        legal_entity_id=legal_entity_id,
        bank_name=chosen.bank_name,
        account_number=open_sealed(chosen.tenant_id, chosen.account_number),
        ifsc=chosen.ifsc,
        swift=chosen.swift,
        currency=chosen.currency,
    )


# ================================================================ numbering series
def _series_out(row: DocumentSeries, preview: str | None = None) -> DocumentSeriesOut:
    out = DocumentSeriesOut.model_validate(row)
    return out.model_copy(update={"next_number_preview": preview})


async def list_series(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
) -> list[DocumentSeriesOut]:
    await _get(db, legal_entity_id)
    rows = (
        await db.execute(
            select(DocumentSeries)
            .where(DocumentSeries.legal_entity_id == legal_entity_id)
            .order_by(DocumentSeries.doc_type, DocumentSeries.name)
        )
    ).scalars()
    today = date.today()
    return [_series_out(r, await numbering.preview_next(db, r.id, today)) for r in rows]


def _check_config(prefix: str, fy_format: str, separator: str, pad: int) -> None:
    errors = numbering.validate_series_config(numbering.SeriesConfig(prefix, fy_format, separator, pad))
    if errors:
        raise field_errors_failed(errors)


async def create_series(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    data: DocumentSeriesIn,
) -> DocumentSeriesOut:
    entity = await _get(db, legal_entity_id)
    _check_config(data.prefix, data.fy_format, data.separator, data.pad)
    if data.gst_registration_id is None and entity.country == "IN":
        raise ValidationFailed(
            "Indian legal entities number documents per GST registration",
            code="series_registration_required",
            details={"field": "gst_registration_id"},
        )
    if data.gst_registration_id is not None:
        registration = await _registration(db, data.gst_registration_id)
        if registration.legal_entity_id != legal_entity_id:
            raise ValidationFailed(
                "The GST registration belongs to another legal entity", code="series_registration_mismatch"
            )
    row = DocumentSeries(
        id=uuid.uuid4(),
        tenant_id=entity.tenant_id,
        legal_entity_id=legal_entity_id,
        status="active",
        **data.model_dump(),
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "An active series already exists for this registration and document type", code="series_exists"
        ) from exc
    await db.refresh(row)
    return _series_out(row, await numbering.preview_next(db, row.id, date.today()))


async def update_series(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    series_id: uuid.UUID,
    data: DocumentSeriesUpdate,
) -> DocumentSeriesOut:
    row = (
        await db.execute(select(DocumentSeries).where(DocumentSeries.id == series_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Numbering series not found")
    changes = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    config_keys = {"prefix", "fy_format", "separator", "pad"}
    config_changes = {k: v for k, v in changes.items() if k in config_keys and getattr(row, k) != v}
    if config_changes:
        if await numbering.counters_in_use(db, row.id):
            raise ConflictError(
                "The format of a series cannot change once numbers were issued", code="series_in_use"
            )
        merged = {k: config_changes.get(k, getattr(row, k)) for k in config_keys}
        _check_config(merged["prefix"], merged["fy_format"], merged["separator"], int(merged["pad"]))
    for key, value in changes.items():
        setattr(row, key, value)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "An active series already exists for this registration and document type", code="series_exists"
        ) from exc
    await db.refresh(row)
    return _series_out(row, await numbering.preview_next(db, row.id, date.today()))


async def series_for(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    legal_entity_id: uuid.UUID,
    gst_registration_id: uuid.UUID | None,
    doc_type: str,
) -> uuid.UUID:
    stmt = select(DocumentSeries.id).where(
        DocumentSeries.legal_entity_id == legal_entity_id,
        DocumentSeries.doc_type == doc_type,
        DocumentSeries.status == "active",
        DocumentSeries.is_legacy.is_(False),
    )
    stmt = stmt.where(
        DocumentSeries.gst_registration_id.is_(None)
        if gst_registration_id is None
        else DocumentSeries.gst_registration_id == gst_registration_id
    )
    found = (await db.execute(stmt.order_by(DocumentSeries.created_at).limit(1))).scalar_one_or_none()
    if found is None:
        raise ConflictError(
            "No active numbering series is set up for this document type",
            code="series_missing",
            details={"doc_type": doc_type},
        )
    return found


# ================================================================ periods (read)
async def list_entity_periods(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    year: int | None,
) -> list[Any]:
    await _get(db, legal_entity_id)
    return await periods.list_periods(db, legal_entity_id, year)


# ================================================================ Epic 14: admin deltas (FR-76)
async def _table_exists(db: AsyncSession, name: str) -> bool:
    """Tables owned by later epics (invoices, pay periods, ...) may not exist yet in this deployment."""
    return bool((await db.execute(select(func.to_regclass(name)))).scalar_one_or_none())


async def _has_documents(db: AsyncSession, legal_entity_id: uuid.UUID) -> bool:
    for name in _DOCUMENT_TABLES:
        if not await _table_exists(db, name):
            continue
        t = table(name, column("legal_entity_id"))
        if (
            await db.execute(select(1).select_from(t).where(t.c.legal_entity_id == legal_entity_id).limit(1))
        ).first():
            return True
    return False


async def _check_field_locks(db: AsyncSession, entity: LegalEntity, changes: dict[str, Any]) -> None:
    """409 ``legal_entity_field_locked`` when timezone / state / country / currency change after any invoice
    or note was raised for the entity."""
    changed = [
        f
        for f in _LOCKED_FIELDS
        if f in changes and changes[f] is not None and changes[f] != getattr(entity, f)
    ]
    if changed and await _has_documents(db, entity.id):
        raise ConflictError(
            "These fields cannot change once invoices or notes exist for the legal entity",
            code="legal_entity_field_locked",
            details={"fields": changed},
        )


async def _check_pan_against_registrations(
    db: AsyncSession, legal_entity_id: uuid.UUID, pan: str | None
) -> None:
    rows = (
        await db.execute(
            select(GstRegistration.gstin).where(
                GstRegistration.legal_entity_id == legal_entity_id, GstRegistration.status == "active"
            )
        )
    ).scalars()
    clashing = sorted(g for g in rows if pan is None or g[2:12] != pan)
    if clashing:
        raise ValidationFailed(
            "The PAN does not match the active GST registrations of this legal entity",
            code="pan_mismatch",
            details={"field": "pan", "gstins": clashing},
        )


async def _closed_periods(db: AsyncSession, legal_entity_id: uuid.UUID) -> bool:
    from app.models.legal_entities import AccountingPeriod

    return (
        await db.execute(
            select(1)
            .where(AccountingPeriod.legal_entity_id == legal_entity_id, AccountingPeriod.status == "closed")
            .limit(1)
        )
    ).first() is not None


async def _check_one_active_per_state(
    db: AsyncSession, legal_entity_id: uuid.UUID, state_code: str, exclude: uuid.UUID | None
) -> None:
    stmt = select(GstRegistration.id).where(
        GstRegistration.legal_entity_id == legal_entity_id,
        GstRegistration.state_code == state_code,
        GstRegistration.status == "active",
    )
    if exclude is not None:
        stmt = stmt.where(GstRegistration.id != exclude)
    if (await db.execute(stmt.limit(1))).first() is not None:
        raise ConflictError(
            "This legal entity already has an active GST registration in that state",
            code="gst_registration_state_exists",
            details={"state_code": state_code},
        )


def _currency_match(currency: str | None) -> Any:
    return (
        LegalEntityBankAccount.currency.is_(None)
        if currency is None
        else LegalEntityBankAccount.currency == currency
    )


async def _default_account_id(
    db: AsyncSession, legal_entity_id: uuid.UUID, currency: str | None
) -> uuid.UUID | None:
    stmt = select(LegalEntityBankAccount.id).where(
        LegalEntityBankAccount.legal_entity_id == legal_entity_id,
        LegalEntityBankAccount.is_default.is_(True),
        LegalEntityBankAccount.archived_at.is_(None),
        _currency_match(currency),
    )
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


async def _clear_default(
    db: AsyncSession, legal_entity_id: uuid.UUID, currency: str | None, *, keep: uuid.UUID | None
) -> None:
    stmt = (
        update(LegalEntityBankAccount)
        .where(
            LegalEntityBankAccount.legal_entity_id == legal_entity_id,
            LegalEntityBankAccount.is_default.is_(True),
            _currency_match(currency),
        )
        .values(is_default=False)
    )
    if keep is not None:
        stmt = stmt.where(LegalEntityBankAccount.id != keep)
    await db.execute(stmt)


async def readiness_missing(db: AsyncSession, entity: LegalEntity) -> list[str]:
    """Return what an entity still lacks before its first invoice (FR-76): PAN, state, (country IN) a principal GST
    registration, a default bank account and a signatory."""
    missing: list[str] = []
    if not entity.pan:
        missing.append("pan")
    if not entity.state_code:
        missing.append("state_code")
    if entity.country == "IN" and await _principal_registration(db, entity.id) is None:
        missing.append("principal_gst_registration")
    has_account = (
        await db.execute(
            select(LegalEntityBankAccount.id)
            .where(
                LegalEntityBankAccount.legal_entity_id == entity.id,
                LegalEntityBankAccount.is_default.is_(True),
                LegalEntityBankAccount.archived_at.is_(None),
            )
            .limit(1)
        )
    ).first() is not None
    if not has_account:
        missing.append("bank_account")
    if not entity.signatory_name:
        missing.append("signatory")
    return missing


async def readiness(
    ctx: RequestContext,
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
) -> LegalEntityReadinessOut:
    entity = await _get(db, legal_entity_id)
    missing = await readiness_missing(db, entity)
    return LegalEntityReadinessOut(
        legal_entity_id=entity.id,
        ready_for_invoicing=not missing and entity.status == "active",
        missing=missing,
    )


async def set_registration_status(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    gst_registration_id: uuid.UUID,
    *,
    active: bool,
) -> GstRegistrationOut:
    row = await _registration(db, gst_registration_id, lock=True)
    target = "active" if active else "inactive"
    if row.status != target:
        if active:
            await _check_one_active_per_state(db, row.legal_entity_id, row.state_code, row.id)
        row.status = target
        if not active and row.is_principal:
            row.is_principal = False
        await db.flush()
        action = "gst_registration.reactivated" if active else "gst_registration.deactivated"
        audit.record(action, "gst_registration", row.id, session=db)
        emit(
            db,
            GstRegistrationChanged(
                tenant_id=row.tenant_id, gst_registration_id=row.id, legal_entity_id=row.legal_entity_id
            ),
        )
    await db.refresh(row)
    return GstRegistrationOut.model_validate(row)


async def list_luts(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    gst_registration_id: uuid.UUID,
) -> list[LutOut]:
    await _registration(db, gst_registration_id)
    rows = (
        await db.execute(
            select(GstRegistrationLut)
            .where(GstRegistrationLut.gst_registration_id == gst_registration_id)
            .order_by(GstRegistrationLut.fiscal_year.desc())
        )
    ).scalars()
    return [LutOut.model_validate(r) for r in rows]


async def put_lut(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    gst_registration_id: uuid.UUID,
    fiscal_year: str,
    data: LutPutIn,
) -> LutOut:
    """Create or replace the LUT of one financial year (``PUT /gst-registrations/{id}/luts/{fy}``)."""
    registration = await _registration(db, gst_registration_id)
    if data.valid_to < data.valid_from:
        raise ValidationFailed("valid_to must not be before valid_from", code="lut_validity")
    row = (
        await db.execute(
            select(GstRegistrationLut)
            .where(
                GstRegistrationLut.gst_registration_id == gst_registration_id,
                GstRegistrationLut.fiscal_year == fiscal_year,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = GstRegistrationLut(
            id=uuid.uuid4(),
            tenant_id=registration.tenant_id,
            gst_registration_id=registration.id,
            fiscal_year=fiscal_year,
            lut_number=data.lut_number,
            valid_from=data.valid_from,
            valid_to=data.valid_to,
        )
        db.add(row)
    else:
        row.lut_number = data.lut_number
        row.valid_from = data.valid_from
        row.valid_to = data.valid_to
    await db.flush()
    return LutOut.model_validate(row)


async def _bank_account(
    db: AsyncSession, bank_account_id: uuid.UUID, *, lock: bool = False
) -> LegalEntityBankAccount:
    stmt = select(LegalEntityBankAccount).where(LegalEntityBankAccount.id == bank_account_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Bank account not found")
    return row


async def update_bank_account(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    bank_account_id: uuid.UUID,
    data: BankAccountUpdate,
) -> BankAccountOut:
    row = await _bank_account(db, bank_account_id, lock=True)
    changes = data.model_dump(exclude_unset=True)
    archived = changes.pop("archived", None)
    make_default = changes.pop("is_default", None)
    number = changes.pop("account_number", None)
    if number:
        set_identity_field(row, "account_number", number, row.tenant_id)
    if changes.get("currency") is not None:
        await _check_reference(db, state_code=None, currency=changes["currency"], timezone=None)
    for key, value in changes.items():
        if key in {"label", "bank_name"} and value is None:
            continue
        setattr(row, key, value)
    if archived is True and row.archived_at is None:
        row.archived_at = datetime.now(UTC)
        row.is_default = False
    elif archived is False:
        row.archived_at = None
    if make_default is True and archived is not True:
        await _clear_default(db, row.legal_entity_id, row.currency, keep=row.id)
        row.is_default = True
    elif make_default is False:
        row.is_default = False
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "Another account is already the default for this currency", code="bank_account_default_exists"
        ) from exc
    await db.refresh(row)
    return _bank_out(row)


async def unmask_bank_account(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    bank_account_id: uuid.UUID,
) -> BankAccountUnmaskedOut:
    """Decrypt one account number for a holder of ``identity:view`` (AD-35); always audited."""
    row = await _bank_account(db, bank_account_id)
    audit.record("bank_account.unmasked", "legal_entity_bank_account", row.id, session=db)
    return BankAccountUnmaskedOut(id=row.id, account_number=open_sealed(row.tenant_id, row.account_number))


LOGO_MAX_MB = 1


async def upload_logo(
    ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, upload: Any
) -> LegalEntityOut:
    """PNG / JPEG <= 1 MB through the shared upload pipeline (``core.storage`` + ``core.scanning``, AD-13)."""
    from app.services import files_service

    entity = await _get(db, legal_entity_id, lock=True)
    data = await files_service.read_upload(upload, LOGO_MAX_MB)
    stored = await files_service.store_upload(
        ctx, db, data, getattr(upload, "filename", None), ("png", "jpeg"), "legal_entity", entity.id
    )
    entity.logo_file_id = stored.id
    await db.flush()
    await db.refresh(entity)
    return LegalEntityOut.model_validate(entity)


async def list_gst_states(ctx: RequestContext, db: AsyncSession) -> list[GstStateOut]:  # ruff: ignore[unused-function-argument]
    rows = (await db.execute(select(GstState).order_by(GstState.name))).scalars()
    return [GstStateOut.model_validate(r) for r in rows]


async def list_currencies(ctx: RequestContext, db: AsyncSession) -> list[CurrencyOut]:  # ruff: ignore[unused-function-argument]
    rows = (await db.execute(select(Currency).order_by(Currency.code))).scalars()
    return [CurrencyOut.model_validate(r) for r in rows]
