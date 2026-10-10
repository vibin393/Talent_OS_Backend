"""Tags and saved candidate lists (FR-25, story 3.7).

* Tags are tenant-wide, case-insensitive (``citext``) and created on first use.
* Lists are visible to their owner, the users they are shared with and members of shared teams. Only the
  owner edits, archives or shares a list; shared users may add and remove members. Removing a member deletes
  only the membership row.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import exists, false, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, forbidden
from app.models.candidates import CandidateList, CandidateListMember, CandidateListShare, CandidateTag, Tag
from app.schemas.candidates import (
    BulkTagsIn,
    BulkTagsOut,
    CandidateListCreate,
    CandidateListOut,
    CandidateListUpdate,
    ListMembersIn,
    ListSharesIn,
    TagNamesIn,
    TagOut,
)
from app.services.candidates.common import get_editable, get_visible, now, principal_of


def _tag_out(tag: Tag) -> TagOut:
    return TagOut(id=tag.id, name=tag.name, color=tag.color)


def _like(term: str) -> str:
    escaped = term.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ================================================================ tags
async def list_tags(ctx: RequestContext, db: AsyncSession, q: str | None, params: PageParams) -> Page[TagOut]:  # noqa: ARG001
    stmt = select(Tag)
    if q and q.strip():
        stmt = stmt.where(Tag.name.ilike(_like(q), escape="\\"))
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (await db.execute(stmt.order_by(Tag.name).limit(params.page_size).offset(params.offset))).scalars()
    return Page[TagOut](
        items=[_tag_out(t) for t in rows], total=total, page=params.page, page_size=params.page_size
    )


async def get_or_create_tags(ctx: RequestContext, db: AsyncSession, names: Sequence[str]) -> list[Tag]:
    out: list[Tag] = []
    seen: set[str] = set()
    for raw in names:
        name = " ".join(raw.split())[:50]
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        tag = (
            await db.execute(select(Tag).where(Tag.name == name))
        ).scalar_one_or_none()  # citext: case-insensitive
        if tag is None:
            tag = Tag(id=uuid.uuid4(), tenant_id=ctx.tenant_id, name=name)
            try:
                async with db.begin_nested():
                    db.add(tag)
                    await db.flush()
            except IntegrityError:
                tag = (await db.execute(select(Tag).where(Tag.name == name))).scalar_one()
        out.append(tag)
    return out


async def attach_tags(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, names: Sequence[str]
) -> list[Tag]:
    tags = await get_or_create_tags(ctx, db, names)
    have = set(
        (
            await db.execute(select(CandidateTag.tag_id).where(CandidateTag.candidate_id == candidate_id))
        ).scalars()
    )
    for tag in tags:
        if tag.id not in have:
            db.add(CandidateTag(tenant_id=ctx.tenant_id, candidate_id=candidate_id, tag_id=tag.id))
            have.add(tag.id)
    await db.flush()
    return tags


async def candidate_tags(db: AsyncSession, candidate_id: uuid.UUID) -> list[TagOut]:
    rows = (
        await db.execute(
            select(Tag)
            .join(CandidateTag, CandidateTag.tag_id == Tag.id)
            .where(CandidateTag.candidate_id == candidate_id)
            .order_by(Tag.name)
        )
    ).scalars()
    return [_tag_out(t) for t in rows]


async def add_tags(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, body: TagNamesIn
) -> list[TagOut]:
    await get_editable(ctx, db, candidate_id)
    await attach_tags(ctx, db, candidate_id, body.names)
    return await candidate_tags(db, candidate_id)


async def remove_tag(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, tag_id: uuid.UUID
) -> list[TagOut]:
    await get_editable(ctx, db, candidate_id)
    row = (
        await db.execute(
            select(CandidateTag).where(
                CandidateTag.candidate_id == candidate_id, CandidateTag.tag_id == tag_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Tag not found on this candidate")
    await db.delete(row)
    await db.flush()
    return await candidate_tags(db, candidate_id)


async def bulk_tags(ctx: RequestContext, db: AsyncSession, body: BulkTagsIn) -> BulkTagsOut:
    ids = list(dict.fromkeys(body.candidate_ids))
    for cid in ids:  # all-or-nothing: 404 / 403 before anything changes
        await get_editable(ctx, db, cid)
    tags: list[Tag] = []
    for cid in ids:
        tags = await attach_tags(ctx, db, cid, body.names)
    return BulkTagsOut(tags=[_tag_out(t) for t in tags], updated=len(ids))


# ================================================================ lists
def visible_list_ids(principal: Principal) -> Any:
    """Subquery of list ids ``principal`` may see (owner, shared user, member of a shared team)."""
    shared = select(CandidateListShare.list_id).where(
        or_(
            CandidateListShare.user_id == principal.id,
            CandidateListShare.team_id.in_(list(principal.team_ids)) if principal.team_ids else false(),
        )
    )
    return select(CandidateList.id).where(
        or_(CandidateList.owner_id == principal.id, CandidateList.id.in_(shared))
    )


async def _list_out(db: AsyncSession, row: CandidateList, principal: Principal) -> CandidateListOut:
    count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(CandidateListMember)
                .where(CandidateListMember.list_id == row.id)
            )
        ).scalar_one()
    )
    shares = list(
        (await db.execute(select(CandidateListShare).where(CandidateListShare.list_id == row.id))).scalars()
    )
    return CandidateListOut(
        id=row.id,
        name=row.name,
        description=row.description,
        owner_id=row.owner_id,
        archived_at=row.archived_at,
        member_count=count,
        shared_user_ids=[s.user_id for s in shares if s.user_id],
        shared_team_ids=[s.team_id for s in shares if s.team_id],
        is_owner=row.owner_id == principal.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _get_list(
    ctx: RequestContext, db: AsyncSession, list_id: uuid.UUID, *, owner: bool = False
) -> CandidateList:
    principal = principal_of(ctx)
    row = (
        await db.execute(
            select(CandidateList).where(
                CandidateList.id == list_id, CandidateList.id.in_(visible_list_ids(principal))
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("List not found")
    if owner and row.owner_id != principal.id:
        raise forbidden("candidate:read")
    return row


async def list_lists(
    ctx: RequestContext, db: AsyncSession, params: PageParams, *, include_archived: bool = False
) -> Page[CandidateListOut]:
    principal = principal_of(ctx)
    stmt = select(CandidateList).where(CandidateList.id.in_(visible_list_ids(principal)))
    if not include_archived:
        stmt = stmt.where(CandidateList.archived_at.is_(None))
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (
        await db.execute(
            stmt.order_by(CandidateList.name, CandidateList.id).limit(params.page_size).offset(params.offset)
        )
    ).scalars()
    return Page[CandidateListOut](
        items=[await _list_out(db, r, principal) for r in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


async def create_list(ctx: RequestContext, db: AsyncSession, body: CandidateListCreate) -> CandidateListOut:
    principal = principal_of(ctx)
    row = CandidateList(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        name=body.name,
        description=body.description,
        owner_id=principal.id,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return await _list_out(db, row, principal)


async def get_list(ctx: RequestContext, db: AsyncSession, list_id: uuid.UUID) -> CandidateListOut:
    return await _list_out(db, await _get_list(ctx, db, list_id), principal_of(ctx))


async def update_list(
    ctx: RequestContext, db: AsyncSession, list_id: uuid.UUID, body: CandidateListUpdate
) -> CandidateListOut:
    row = await _get_list(ctx, db, list_id, owner=True)
    if "name" in body.model_fields_set and body.name:
        row.name = body.name
    if "description" in body.model_fields_set:
        row.description = body.description
    await db.flush()
    await db.refresh(row)
    return await _list_out(db, row, principal_of(ctx))


async def archive_list(ctx: RequestContext, db: AsyncSession, list_id: uuid.UUID) -> CandidateListOut:
    row = await _get_list(ctx, db, list_id, owner=True)
    if row.archived_at is None:
        row.archived_at = now()
        await db.flush()
    await db.refresh(row)
    return await _list_out(db, row, principal_of(ctx))


async def set_shares(
    ctx: RequestContext, db: AsyncSession, list_id: uuid.UUID, body: ListSharesIn
) -> CandidateListOut:
    row = await _get_list(ctx, db, list_id, owner=True)
    for share in (
        await db.execute(select(CandidateListShare).where(CandidateListShare.list_id == row.id))
    ).scalars():
        await db.delete(share)
    await db.flush()
    try:
        async with db.begin_nested():
            for uid in dict.fromkeys(body.user_ids):
                if uid != row.owner_id:
                    db.add(CandidateListShare(tenant_id=ctx.tenant_id, list_id=row.id, user_id=uid))
            for tid in dict.fromkeys(body.team_ids):
                db.add(CandidateListShare(tenant_id=ctx.tenant_id, list_id=row.id, team_id=tid))
            await db.flush()
    except IntegrityError as exc:
        raise ValidationFailed(
            "Unknown user or team",
            code="invalid_reference",
            details={"fields": [{"field": "user_ids/team_ids"}]},
        ) from exc
    return await _list_out(db, row, principal_of(ctx))


async def add_members(
    ctx: RequestContext, db: AsyncSession, list_id: uuid.UUID, body: ListMembersIn
) -> CandidateListOut:
    row = await _get_list(ctx, db, list_id)
    ids = list(dict.fromkeys(body.candidate_ids))
    for cid in ids:
        await get_visible(ctx, db, cid)
    have = set(
        (
            await db.execute(
                select(CandidateListMember.candidate_id).where(CandidateListMember.list_id == row.id)
            )
        ).scalars()
    )
    principal = principal_of(ctx)
    for cid in ids:
        if cid not in have:
            db.add(
                CandidateListMember(
                    tenant_id=ctx.tenant_id, list_id=row.id, candidate_id=cid, added_by=principal.id
                )
            )
    await db.flush()
    return await _list_out(db, row, principal)


async def remove_member(
    ctx: RequestContext, db: AsyncSession, list_id: uuid.UUID, candidate_id: uuid.UUID
) -> CandidateListOut:
    row = await _get_list(ctx, db, list_id)
    member = (
        await db.execute(
            select(CandidateListMember).where(
                CandidateListMember.list_id == row.id, CandidateListMember.candidate_id == candidate_id
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise NotFound("Candidate is not on this list")
    await db.delete(member)
    await db.flush()
    return await _list_out(db, row, principal_of(ctx))


async def is_member_of_visible_list(db: AsyncSession, principal: Principal, candidate_id: uuid.UUID) -> bool:
    stmt = select(
        exists().where(
            CandidateListMember.candidate_id == candidate_id,
            CandidateListMember.list_id.in_(visible_list_ids(principal)),
        )
    )
    return bool((await db.execute(stmt)).scalar())
