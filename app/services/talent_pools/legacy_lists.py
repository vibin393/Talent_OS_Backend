# ruff: file-ignore[line-too-long, non-imperative-mood]
"""P1 saved lists as static Pools (decision 11, spec A4).

Migration ``302_02`` turned every ``candidate_lists`` row into a private static Pool (``legacy_list_id``). The
P1 ``/candidate-lists`` endpoints are now **aliases** that call this module and keep their P1 request / response
schemas and access rules (owner edits / archives / shares; shared users add and remove members):

* writes go to the Pool first and are mirrored **down** to the P1 tables in the same transaction
  (``mirror_pool``, ``members.add_members`` / ``remove_member``), so P1 reads (records list filter, Candidate 360
  "Lists") keep working unchanged;
* a create makes the P1 list row and its Pool together;
* a list without a Pool (created by a P1 code path after the migration) gets one lazily (``ensure_pool``).

The P1 tables are frozen for new features; retiring them is a Phase 4 item.
"""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.pagination import Page, PageParams
from app.core.permissions import forbidden
from app.models.candidates import CandidateList, CandidateListMember, CandidateListShare
from app.models.talent_pools import TalentPool, TalentPoolMember, TalentPoolShare
from app.schemas.candidates import (
    CandidateListCreate,
    CandidateListOut,
    CandidateListUpdate,
    ListMembersIn,
    ListSharesIn,
)
from app.services.candidates import tags_lists
from app.services.candidates.common import principal_of
from app.services.talent_pools import common, members

LIST_PERMISSION = "candidate:read"


async def ensure_pool(db: AsyncSession, row: CandidateList) -> TalentPool:
    """The Pool of a P1 list (created and back-filled when missing; idempotent on ``legacy_list_id``)."""
    pool = (
        await db.execute(select(TalentPool).where(TalentPool.legacy_list_id == row.id))
    ).scalar_one_or_none()
    if pool is not None:
        return pool
    pool = TalentPool(
        id=uuid.uuid4(),
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        kind="static",
        owner_id=row.owner_id,
        visibility="private",
        status="archived" if row.archived_at else "active",
        archived_at=row.archived_at,
        legacy_list_id=row.id,
        member_count=0,
    )
    db.add(pool)
    await db.flush()
    list_members = (
        await db.execute(select(CandidateListMember).where(CandidateListMember.list_id == row.id))
    ).scalars()
    count = 0
    for m in list_members:
        db.add(
            TalentPoolMember(
                tenant_id=row.tenant_id,
                pool_id=pool.id,
                candidate_id=m.candidate_id,
                source="legacy_list",
                reason={"legacy_list_id": str(row.id)},
                added_by=m.added_by,
                added_at=m.created_at,
            )
        )
        count += 1
    for s in (
        await db.execute(select(CandidateListShare).where(CandidateListShare.list_id == row.id))
    ).scalars():
        db.add(
            TalentPoolShare(tenant_id=row.tenant_id, pool_id=pool.id, user_id=s.user_id, team_id=s.team_id)
        )
    pool.member_count = count
    await db.flush()
    return pool


async def mirror_pool(db: AsyncSession, pool: TalentPool) -> None:
    """Pool -> P1 list row (name, description, archive state)."""
    if pool.legacy_list_id is None:
        return
    row = await db.get(CandidateList, pool.legacy_list_id)
    if row is None:
        return
    row.name = pool.name
    row.description = pool.description
    row.archived_at = pool.archived_at if pool.status == "archived" else None
    await db.flush()


async def _resolve(
    ctx: RequestContext, db: AsyncSession, list_id: uuid.UUID, *, owner: bool = False
) -> tuple[CandidateList, TalentPool]:
    principal = principal_of(ctx)
    row = (
        await db.execute(
            select(CandidateList).where(
                CandidateList.id == list_id, CandidateList.id.in_(tags_lists.visible_list_ids(principal))
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("List not found")
    if owner and row.owner_id != principal.id:
        raise forbidden(LIST_PERMISSION)
    return row, await ensure_pool(db, row)


async def _backfill(ctx: RequestContext, db: AsyncSession) -> None:
    principal = principal_of(ctx)
    orphan = (
        await db.execute(
            select(CandidateList).where(
                CandidateList.id.in_(tags_lists.visible_list_ids(principal)),
                CandidateList.id.not_in(
                    select(TalentPool.legacy_list_id).where(TalentPool.legacy_list_id.is_not(None))
                ),
            )
        )
    ).scalars()
    for row in list(orphan):
        await ensure_pool(db, row)


# ---------------------------------------------------------------- alias endpoints (P1 schemas)
async def list_lists(
    ctx: RequestContext, db: AsyncSession, params: PageParams, *, include_archived: bool = False
) -> Page[CandidateListOut]:
    await _backfill(ctx, db)
    return await tags_lists.list_lists(ctx, db, params, include_archived=include_archived)


async def create_list(ctx: RequestContext, db: AsyncSession, body: CandidateListCreate) -> CandidateListOut:
    created = await tags_lists.create_list(ctx, db, body)
    row = await db.get(CandidateList, created.id)
    assert row is not None
    await ensure_pool(db, row)
    return created


async def get_list(ctx: RequestContext, db: AsyncSession, list_id: uuid.UUID) -> CandidateListOut:
    await _resolve(ctx, db, list_id)
    return await tags_lists.get_list(ctx, db, list_id)


async def update_list(
    ctx: RequestContext, db: AsyncSession, list_id: uuid.UUID, body: CandidateListUpdate
) -> CandidateListOut:
    _, pool = await _resolve(ctx, db, list_id, owner=True)
    if "name" in body.model_fields_set and body.name:
        pool.name = body.name
    if "description" in body.model_fields_set:
        pool.description = body.description
    await db.flush()
    await mirror_pool(db, pool)
    return await tags_lists.get_list(ctx, db, list_id)


async def archive_list(ctx: RequestContext, db: AsyncSession, list_id: uuid.UUID) -> CandidateListOut:
    _, pool = await _resolve(ctx, db, list_id, owner=True)
    if pool.status != "archived":
        pool.status = "archived"
        pool.archived_at = common.now()
        await db.flush()
        await mirror_pool(db, pool)
    return await tags_lists.get_list(ctx, db, list_id)


async def set_shares(
    ctx: RequestContext, db: AsyncSession, list_id: uuid.UUID, body: ListSharesIn
) -> CandidateListOut:
    row, pool = await _resolve(ctx, db, list_id, owner=True)
    await db.execute(delete(TalentPoolShare).where(TalentPoolShare.pool_id == pool.id))
    await db.execute(delete(CandidateListShare).where(CandidateListShare.list_id == row.id))
    await db.flush()
    try:
        async with db.begin_nested():
            for uid in dict.fromkeys(body.user_ids):
                if uid != row.owner_id:
                    db.add(TalentPoolShare(tenant_id=row.tenant_id, pool_id=pool.id, user_id=uid))
                    db.add(CandidateListShare(tenant_id=row.tenant_id, list_id=row.id, user_id=uid))
            for tid in dict.fromkeys(body.team_ids):
                db.add(TalentPoolShare(tenant_id=row.tenant_id, pool_id=pool.id, team_id=tid))
                db.add(CandidateListShare(tenant_id=row.tenant_id, list_id=row.id, team_id=tid))
            await db.flush()
    except IntegrityError as exc:
        raise ValidationFailed(
            "Unknown user or team",
            code="invalid_reference",
            details={"fields": [{"field": "user_ids/team_ids"}]},
        ) from exc
    return await tags_lists.get_list(ctx, db, list_id)


async def add_members(
    ctx: RequestContext, db: AsyncSession, list_id: uuid.UUID, body: ListMembersIn
) -> CandidateListOut:
    _, pool = await _resolve(ctx, db, list_id)
    principal = principal_of(ctx)
    await members.add_members(
        ctx, db, pool, list(body.candidate_ids), source="manual", actor_id=principal.id, allow_archived=True
    )
    return await tags_lists.get_list(ctx, db, list_id)


async def remove_member(
    ctx: RequestContext, db: AsyncSession, list_id: uuid.UUID, candidate_id: uuid.UUID
) -> CandidateListOut:
    _, pool = await _resolve(ctx, db, list_id)
    try:
        await members.remove_member(ctx, db, pool, candidate_id, allow_archived=True)
    except NotFound as exc:
        raise NotFound("Candidate is not on this list") from exc
    return await tags_lists.get_list(ctx, db, list_id)
