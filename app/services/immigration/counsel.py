# ruff: file-ignore[line-too-long]
"""Immigration counsel directory (Epic 46 A2). Counsel are contacts, not users: P1 ``contacts`` require a Client,
so counsel live in ``immigration_counsel`` (recorded deviation). Reads ``immigration:read``; writes
``immigration:configure``."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.immigration import ImmigrationCounsel
from app.schemas.immigration import CounselIn, CounselOut, CounselPatch
from app.services.immigration.common import IMM_CONFIGURE, IMM_READ, not_found, now, require, tenant_of


async def list_counsel(
    ctx: RequestContext, db: AsyncSession, *, include_archived: bool = False
) -> list[CounselOut]:
    require(ctx, IMM_READ)
    stmt = select(ImmigrationCounsel).order_by(ImmigrationCounsel.name)
    if not include_archived:
        stmt = stmt.where(ImmigrationCounsel.archived_at.is_(None))
    return [CounselOut.model_validate(c) for c in (await db.execute(stmt)).scalars()]


async def create(ctx: RequestContext, db: AsyncSession, data: CounselIn) -> CounselOut:
    require(ctx, IMM_CONFIGURE)
    c = ImmigrationCounsel(id=uuid.uuid4(), tenant_id=tenant_of(db), **data.model_dump())
    db.add(c)
    await db.flush()
    return CounselOut.model_validate(c)


async def update(
    ctx: RequestContext, db: AsyncSession, counsel_id: uuid.UUID, data: CounselPatch
) -> CounselOut:
    require(ctx, IMM_CONFIGURE)
    c = await db.get(ImmigrationCounsel, counsel_id)
    if c is None:
        raise not_found("Counsel")
    fields = data.model_dump(exclude_unset=True)
    archived = fields.pop("archived", None)
    for k, v in fields.items():
        if k == "name" and not v:
            continue
        setattr(c, k, v)
    if archived is not None:
        c.archived_at = now() if archived else None
    await db.flush()
    return CounselOut.model_validate(c)
