"""Referral programs and settings (Epic 42 story 42.1, FR-213): CRUD under ``referral:configure``."""

import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.models.referrals import ReferralProgram
from app.schemas.referrals import ProgramIn, ProgramOut, ProgramPatch, SettingsIn, SettingsOut
from app.services.referrals.common import REF_CONFIGURE, get_settings_row, invalid, not_found, now, require

JSON_FIELDS = ("eligible_demands", "reward_rules", "exclusions")


def out(row: ReferralProgram) -> ProgramOut:
    return ProgramOut.model_validate(row)


def _values(data: ProgramIn | ProgramPatch, *, exclude_unset: bool) -> dict[str, Any]:
    raw = data.model_dump(mode="json", exclude_unset=exclude_unset)
    values = data.model_dump(exclude_unset=exclude_unset)
    for f in JSON_FIELDS:  # JSONB columns store the JSON form (Decimal amounts as strings)
        if f in raw:
            values[f] = raw[f]
    return values


async def list_programs(
    ctx: RequestContext, db: AsyncSession, *, include_archived: bool = False
) -> list[ProgramOut]:
    require(ctx, REF_CONFIGURE)
    stmt = select(ReferralProgram).order_by(ReferralProgram.priority.desc(), ReferralProgram.name)
    if not include_archived:
        stmt = stmt.where(ReferralProgram.archived_at.is_(None))
    return [out(r) for r in (await db.execute(stmt)).scalars()]


async def _get(db: AsyncSession, program_id: uuid.UUID) -> ReferralProgram:
    row = await db.get(ReferralProgram, program_id)
    if row is None:
        raise not_found("Referral program")
    return row


async def get_program(ctx: RequestContext, db: AsyncSession, program_id: uuid.UUID) -> ProgramOut:
    require(ctx, REF_CONFIGURE)
    return out(await _get(db, program_id))


async def create_program(ctx: RequestContext, db: AsyncSession, data: ProgramIn) -> ProgramOut:
    require(ctx, REF_CONFIGURE)
    row = ReferralProgram(id=uuid.uuid4(), tenant_id=ctx.tenant_id, **_values(data, exclude_unset=False))
    db.add(row)
    await db.flush()
    await db.refresh(row)
    audit.record("referral.program_created", "referral_program", row.id, session=db, name=row.name)
    return out(row)


async def update_program(
    ctx: RequestContext, db: AsyncSession, program_id: uuid.UUID, data: ProgramPatch
) -> ProgramOut:
    require(ctx, REF_CONFIGURE)
    row = await _get(db, program_id)
    for key, value in _values(data, exclude_unset=True).items():
        setattr(row, key, value)
    try:  # whole-record validation (validity window, rule keys)
        ProgramIn.model_validate({k: getattr(row, k) for k in ProgramIn.model_fields})
    except ValidationError as exc:
        raise invalid(
            "invalid_program",
            "The program is not valid",
            errors=exc.errors(include_url=False, include_context=False),
        ) from exc
    await db.flush()
    await db.refresh(row)
    return out(row)


async def archive_program(ctx: RequestContext, db: AsyncSession, program_id: uuid.UUID) -> ProgramOut:
    require(ctx, REF_CONFIGURE)
    row = await _get(db, program_id)
    if row.archived_at is None:
        row.archived_at = now()
        row.active = False
    await db.flush()
    await db.refresh(row)
    return out(row)


async def get_settings(ctx: RequestContext, db: AsyncSession) -> SettingsOut:
    require(ctx, REF_CONFIGURE)
    return SettingsOut.model_validate(await get_settings_row(db))


async def put_settings(ctx: RequestContext, db: AsyncSession, data: SettingsIn) -> SettingsOut:
    require(ctx, REF_CONFIGURE)
    row = await get_settings_row(db)
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    return SettingsOut.model_validate(row)
