# ruff: file-ignore[line-too-long]
"""CRM settings, reason codes and lead sources (story 2.1, A1). Rows are created / seeded lazily per tenant."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.crm import DEFAULT_STAGE_PROBABILITIES, CrmLeadSource, CrmReasonCode, CrmSettings
from app.schemas.crm import (
    CrmSettingsOut,
    CrmSettingsUpdate,
    LeadSourceIn,
    LeadSourceOut,
    LeadSourceUpdate,
    ReasonCodeIn,
    ReasonCodeOut,
    ReasonCodeUpdate,
)
from app.services.crm.common import tenant_of

REASON_SEEDS: dict[str, tuple[tuple[str, str], ...]] = {
    "lead_disqualify": (
        ("no_budget", "No budget"),
        ("no_need", "No need"),
        ("not_reachable", "Not reachable"),
        ("competitor_incumbent", "Competitor incumbent"),
        ("bad_fit", "Bad fit"),
        ("duplicate", "Duplicate"),
        ("other", "Other"),
    ),
    "opportunity_lost": (
        ("price", "Price"),
        ("competitor", "Competitor"),
        ("no_decision", "No decision"),
        ("timing", "Timing"),
        ("scope_change", "Scope change"),
        ("relationship", "Relationship"),
        ("other", "Other"),
    ),
    "reopen": (("new_contact", "New contact"), ("need_revived", "Need revived"), ("other", "Other")),
}
SOURCE_SEEDS = (
    ("website", "Website"),
    ("referral", "Referral"),
    ("linkedin", "LinkedIn"),
    ("event", "Event"),
    ("cold_outreach", "Cold outreach"),
    ("partner", "Partner"),
    ("import", "Import"),
    ("other", "Other"),
)
OTHER = "other"


async def ensure_seeded(db: AsyncSession) -> None:
    """Idempotent: settings row, default reason codes and sources for the session's tenant."""
    tenant_id = tenant_of(db)
    if (await db.execute(select(CrmSettings.tenant_id))).first() is not None:
        return
    await db.execute(pg_insert(CrmSettings).values(tenant_id=tenant_id).on_conflict_do_nothing())
    reason_rows = [
        {"id": uuid.uuid4(), "tenant_id": tenant_id, "kind": kind, "code": code, "label": label}
        for kind, seeds in REASON_SEEDS.items()
        for code, label in seeds
    ]
    await db.execute(pg_insert(CrmReasonCode).values(reason_rows).on_conflict_do_nothing())
    source_rows = [
        {"id": uuid.uuid4(), "tenant_id": tenant_id, "code": c, "label": lbl} for c, lbl in SOURCE_SEEDS
    ]
    await db.execute(pg_insert(CrmLeadSource).values(source_rows).on_conflict_do_nothing())


async def _row(db: AsyncSession) -> CrmSettings:
    await ensure_seeded(db)
    return (await db.execute(select(CrmSettings))).scalar_one()


async def stage_probabilities(db: AsyncSession) -> dict[str, int]:
    row = await _row(db)
    return {**DEFAULT_STAGE_PROBABILITIES, **{k: int(v) for k, v in (row.stage_probabilities or {}).items()}}


async def default_currency(db: AsyncSession) -> str:
    return (await _row(db)).default_currency


async def get_settings(ctx: RequestContext, db: AsyncSession) -> CrmSettingsOut:  # ruff: ignore[unused-function-argument]
    row = await _row(db)
    return CrmSettingsOut(
        stage_probabilities=await stage_probabilities(db), default_currency=row.default_currency
    )


async def update_settings(ctx: RequestContext, db: AsyncSession, data: CrmSettingsUpdate) -> CrmSettingsOut:
    row = await _row(db)
    if data.stage_probabilities is not None:
        row.stage_probabilities = {**(row.stage_probabilities or {}), **data.stage_probabilities}
    if data.default_currency is not None:
        row.default_currency = data.default_currency
    await db.flush()
    return await get_settings(ctx, db)


# ---------------------------------------------------------------- reason codes
async def list_reason_codes(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    kind: str | None = None,
    include_archived: bool = False,
) -> list[ReasonCodeOut]:
    await ensure_seeded(db)
    stmt = select(CrmReasonCode)
    if kind:
        stmt = stmt.where(CrmReasonCode.kind == kind)
    if not include_archived:
        stmt = stmt.where(CrmReasonCode.archived_at.is_(None))
    rows = await db.execute(
        stmt.order_by(CrmReasonCode.kind, CrmReasonCode.code == OTHER, CrmReasonCode.label)
    )
    return [ReasonCodeOut.model_validate(r) for r in rows.scalars()]


async def create_reason_code(ctx: RequestContext, db: AsyncSession, data: ReasonCodeIn) -> ReasonCodeOut:  # ruff: ignore[unused-function-argument]
    await ensure_seeded(db)
    clash = await db.execute(
        select(CrmReasonCode.id).where(CrmReasonCode.kind == data.kind, CrmReasonCode.code == data.code)
    )
    if clash.first():
        raise ConflictError("This reason code already exists", code="reason_code_exists")
    row = CrmReasonCode(
        id=uuid.uuid4(), tenant_id=tenant_of(db), kind=data.kind, code=data.code, label=data.label
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return ReasonCodeOut.model_validate(row)


async def update_reason_code(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    code_id: uuid.UUID,
    data: ReasonCodeUpdate,
) -> ReasonCodeOut:
    row = await db.get(CrmReasonCode, code_id)
    if row is None:
        raise NotFound("Reason code not found")
    if data.label is not None:
        row.label = data.label
    if data.archived is not None:
        row.archived_at = datetime.now(UTC) if data.archived else None
    await db.flush()
    await db.refresh(row)
    return ReasonCodeOut.model_validate(row)


async def require_reason(db: AsyncSession, kind: str, code: str | None, note: str | None) -> str:
    """422 ``reason_code_required`` / ``invalid_reason_code`` / ``note_required`` (``other`` needs a note)."""
    from app.services.crm.common import reason_required

    if not code:
        raise reason_required()
    await ensure_seeded(db)
    found = (
        await db.execute(
            select(CrmReasonCode.id).where(
                CrmReasonCode.kind == kind, CrmReasonCode.code == code, CrmReasonCode.archived_at.is_(None)
            )
        )
    ).first()
    if found is None:
        raise ValidationFailed(
            "Unknown reason code",
            code="invalid_reason_code",
            details={
                "fields": [{"field": "reason_code", "message": "Unknown reason code", "type": "invalid"}]
            },
        )
    if code == OTHER and not (note or "").strip():
        raise reason_required("note", "A note is required for the reason 'other'")
    return code


# ---------------------------------------------------------------- lead sources
async def list_sources(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    include_archived: bool = False,
) -> list[LeadSourceOut]:
    await ensure_seeded(db)
    stmt = select(CrmLeadSource)
    if not include_archived:
        stmt = stmt.where(CrmLeadSource.archived_at.is_(None))
    rows = await db.execute(stmt.order_by(CrmLeadSource.code == OTHER, CrmLeadSource.label))
    return [LeadSourceOut.model_validate(r) for r in rows.scalars()]


async def source_codes(db: AsyncSession) -> set[str]:
    await ensure_seeded(db)
    rows = await db.execute(select(CrmLeadSource.code).where(CrmLeadSource.archived_at.is_(None)))
    return set(rows.scalars())


async def create_source(ctx: RequestContext, db: AsyncSession, data: LeadSourceIn) -> LeadSourceOut:  # ruff: ignore[unused-function-argument]
    await ensure_seeded(db)
    if (await db.execute(select(CrmLeadSource.id).where(CrmLeadSource.code == data.code))).first():
        raise ConflictError("This lead source already exists", code="lead_source_exists")
    row = CrmLeadSource(id=uuid.uuid4(), tenant_id=tenant_of(db), code=data.code, label=data.label)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return LeadSourceOut.model_validate(row)


async def update_source(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    source_id: uuid.UUID,
    data: LeadSourceUpdate,
) -> LeadSourceOut:
    row = await db.get(CrmLeadSource, source_id)
    if row is None:
        raise NotFound("Lead source not found")
    if data.label is not None:
        row.label = data.label
    if data.archived is not None:
        row.archived_at = datetime.now(UTC) if data.archived else None
    await db.flush()
    await db.refresh(row)
    return LeadSourceOut.model_validate(row)


async def require_source(db: AsyncSession, code: str | None) -> str | None:
    if code is None:
        return None
    if code not in await source_codes(db):
        raise ValidationFailed(
            "Unknown lead source",
            code="invalid_source",
            details={
                "fields": [{"field": "source_code", "message": "Unknown lead source", "type": "invalid"}]
            },
        )
    return code
