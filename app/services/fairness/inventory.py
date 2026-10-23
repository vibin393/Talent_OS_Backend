# ruff: file-ignore[line-too-long]
"""AEDT inventory (Epic 49 A7, FR-248): AI Match, AI Screening and assessment auto-scoring are seeded per tenant as
``inactive`` until a Compliance user confirms them; edits are audited (``AuditedMixin``)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.fairness import AedtInventory
from app.models.matching import Match
from app.schemas.fairness import AedtIn, AedtOut, AedtPatch
from app.services.fairness.common import not_found, tenant_of

SEED = (
    (
        "ai_match",
        "ai_match",
        "AI Match",
        "Candidate profile (skills, experience, location, compensation fit) against Demand requirements",
    ),
    (
        "ai_screening",
        "ai_screening",
        "AI Screening",
        "Candidate answers to screening questions and the resume text",
    ),
    (
        "assessment_autoscore",
        "assessment_autoscore",
        "Assessment auto-scoring",
        "Candidate answers to objective assessment items",
    ),
)


async def ensure_seed(db: AsyncSession) -> None:
    have = set(
        (
            await db.execute(select(AedtInventory.seed_key).where(AedtInventory.seed_key.is_not(None)))
        ).scalars()
    )
    tenant_id = tenant_of(db)
    for key, feature, name, data_used in SEED:
        if key not in have:
            db.add(
                AedtInventory(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    seed_key=key,
                    feature=feature,
                    name=name,
                    data_used=data_used,
                    used_to_filter=True,
                    status="inactive",
                    job_categories=[],
                )
            )
    await db.flush()
    match_row = (
        await db.execute(select(AedtInventory).where(AedtInventory.seed_key == "ai_match"))
    ).scalar_one_or_none()
    if match_row is not None and match_row.version is None:
        latest = (
            await db.execute(select(Match.scoring_version).order_by(Match.computed_at.desc()).limit(1))
        ).scalar_one_or_none()
        first = (await db.execute(select(func.min(Match.computed_at)))).scalar_one_or_none()
        if latest:
            match_row.version = latest
        if first and match_row.first_used_on is None:
            match_row.first_used_on = first.date()
        await db.flush()
    # Epic 65 (FR-323): the Phase 4 agent entries (inactive until Compliance confirms them).
    from app.services.fairness import aedt_p4

    await aedt_p4.ensure_seed_p4(db)


async def list_entries(ctx: RequestContext, db: AsyncSession) -> list[AedtOut]:  # ruff: ignore[unused-function-argument]
    await ensure_seed(db)
    rows = (
        await db.execute(select(AedtInventory).order_by(AedtInventory.created_at, AedtInventory.name))
    ).scalars()
    return [AedtOut.model_validate(r) for r in rows]


async def load(db: AsyncSession, aedt_id: uuid.UUID) -> AedtInventory:
    row = await db.get(AedtInventory, aedt_id)
    if row is None:
        raise not_found("AEDT inventory entry")
    return row


async def create(ctx: RequestContext, db: AsyncSession, data: AedtIn) -> AedtOut:  # ruff: ignore[unused-function-argument]
    row = AedtInventory(id=uuid.uuid4(), tenant_id=tenant_of(db), **data.model_dump())
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return AedtOut.model_validate(row)


async def update(ctx: RequestContext, db: AsyncSession, aedt_id: uuid.UUID, data: AedtPatch) -> AedtOut:  # ruff: ignore[unused-function-argument]
    row = await load(db, aedt_id)
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is None and key in {"feature", "name", "job_categories", "used_to_filter", "status"}:
            continue
        setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    return AedtOut.model_validate(row)


async def active_entries(db: AsyncSession) -> list[AedtInventory]:
    return list(
        (
            await db.execute(
                select(AedtInventory).where(AedtInventory.status == "active").order_by(AedtInventory.name)
            )
        ).scalars()
    )
