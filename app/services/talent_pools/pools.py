"""Static and dynamic Pools: CRUD, list, subscriptions (Epic 38 story 38.2, FR-192).

* ``pool:write`` covers private and team Pools; ``visibility='tenant'`` needs ``pool:manage``.
* A dynamic Pool names a visible Segment (``segment_id``) or creates one inline (``segment``, needs
  ``segment:write``); creating it starts the initial refresh (``trigger='created'``). At most
  ``max_dynamic_pools`` active dynamic Pools per tenant (NFR-40, 409 ``pool_limit_reached``).
* Archive replaces delete.
"""

import uuid
from collections.abc import Sequence
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.core.permissions import Principal, forbidden
from app.models.identity import User
from app.models.talent_pools import Segment, TalentPool, TalentPoolSubscription
from app.schemas.segments import SegmentCreate
from app.schemas.talent_pools import PoolCreate, PoolDiff, PoolOut, PoolUpdate, SubscriptionOut
from app.services.segments import crud as segments_crud
from app.services.talent_pools import common, refresh
from app.services.talent_pools import settings as pool_settings

ListFilter = Literal["all", "mine", "team", "tenant"]


def _check_visibility(principal: Principal, visibility: str, team_id: uuid.UUID | None) -> uuid.UUID | None:
    if visibility == "tenant" and not principal.has(common.MANAGE):
        raise forbidden(common.MANAGE)
    if visibility == "team":
        if team_id is None and principal.team_ids:
            team_id = sorted(principal.team_ids)[0]  # the caller's team when none is named
        if team_id is None:
            raise ValidationFailed(
                "Choose the team that can see it", code="team_required", details={"path": "team_id"}
            )
        if team_id not in principal.team_ids and not principal.has(common.MANAGE):
            raise ValidationFailed(
                "You can share only with your own teams", code="team_not_member", details={"path": "team_id"}
            )
    return team_id


async def outs(ctx: RequestContext, db: AsyncSession, pools: Sequence[TalentPool]) -> list[PoolOut]:
    if not pools:
        return []
    principal = common.principal_of(ctx)
    seg_ids = {p.segment_id for p in pools if p.segment_id}
    segs = (
        {s.id: s for s in (await db.execute(select(Segment).where(Segment.id.in_(seg_ids)))).scalars()}
        if seg_ids
        else {}
    )
    owners = dict(
        (await db.execute(select(User.id, User.name).where(User.id.in_({p.owner_id for p in pools}))))
        .tuples()
        .all()
    )
    subscribed = set(
        (
            await db.execute(
                select(TalentPoolSubscription.pool_id).where(
                    TalentPoolSubscription.user_id == principal.id,
                    TalentPoolSubscription.pool_id.in_([p.id for p in pools]),
                )
            )
        ).scalars()
    )
    result = []
    for p in pools:
        seg = segs.get(p.segment_id) if p.segment_id else None
        result.append(
            PoolOut(
                id=p.id,
                name=p.name,
                description=p.description,
                kind=p.kind,
                segment_id=p.segment_id,
                segment_name=seg.name if seg else None,
                source_key=seg.source_key
                if seg
                else ("candidates.candidate" if p.kind == "static" else None),
                owner_id=p.owner_id,
                owner_name=owners.get(p.owner_id),
                visibility=p.visibility,
                team_id=p.team_id,
                status=p.status,
                archived_at=p.archived_at,
                template_key=p.template_key,
                legacy_list_id=p.legacy_list_id,
                member_count=p.member_count,
                last_refreshed_at=p.last_refreshed_at,
                last_diff=PoolDiff(**p.last_diff) if p.last_diff else None,
                needs_repair=bool(seg.needs_repair) if seg else False,
                subscribed=p.id in subscribed,
                can_edit=common.can_edit(principal, p),
                created_at=p.created_at,
                updated_at=p.updated_at,
            )
        )
    return result


async def out(ctx: RequestContext, db: AsyncSession, pool: TalentPool) -> PoolOut:
    await db.refresh(pool)
    return (await outs(ctx, db, [pool]))[0]


async def list_pools(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    filter_: ListFilter = "all",
    kind: str | None = None,
    include_archived: bool = False,
    q: str | None = None,
    writable: bool = False,
) -> list[PoolOut]:
    principal = common.principal_of(ctx)
    stmt = select(TalentPool).where(common.visible(principal))
    if filter_ == "mine":
        stmt = stmt.where(TalentPool.owner_id == principal.id)
    elif filter_ == "team":
        stmt = stmt.where(TalentPool.visibility == "team")
    elif filter_ == "tenant":
        stmt = stmt.where(TalentPool.visibility == "tenant")
    if kind in ("static", "dynamic"):
        stmt = stmt.where(TalentPool.kind == kind)
    if not include_archived:
        stmt = stmt.where(TalentPool.status == "active")
    if q and q.strip():
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(TalentPool.name.ilike(f"%{escaped}%", escape="\\"))
    pools = list((await db.execute(stmt.order_by(TalentPool.name, TalentPool.id).limit(2000))).scalars())
    if writable:
        pools = [p for p in pools if p.kind == "static" and await common.can_write_members(db, principal, p)]
    return await outs(ctx, db, pools)


async def _segment_for(
    ctx: RequestContext, db: AsyncSession, principal: Principal, body: PoolCreate
) -> Segment:
    if body.segment_id is not None and body.segment is not None:
        raise ValidationFailed(
            "Send segment_id or segment, not both", code="segment_or_definition", details={"path": "segment"}
        )
    if body.segment_id is not None:
        seg = await segments_crud.get_visible(ctx, db, body.segment_id)
        if seg.archived_at is not None:
            raise ConflictError("This segment is archived", code="segment_archived")
        return seg
    if body.segment is None:
        raise ValidationFailed(
            "A dynamic pool needs a segment", code="segment_required", details={"path": "segment_id"}
        )
    if not principal.has(common.SEGMENT_WRITE):
        raise forbidden(common.SEGMENT_WRITE)
    return await segments_crud.create_segment(
        ctx,
        db,
        SegmentCreate(
            name=body.name,
            description=body.description,
            source_key=body.segment.source_key,
            definition=body.segment.definition,
            visibility=body.visibility,
            team_id=body.team_id,
        ),
        owner_visibility_checked=True,
    )


async def check_dynamic_limit(ctx: RequestContext, db: AsyncSession) -> None:
    assert ctx.tenant_id is not None
    limit = (await pool_settings.get_row(db, ctx.tenant_id)).max_dynamic_pools
    active = int(
        (
            await db.execute(
                select(func.count())
                .select_from(TalentPool)
                .where(TalentPool.kind == "dynamic", TalentPool.status == "active")
            )
        ).scalar_one()
    )
    if active >= limit:
        raise ConflictError(
            f"This tenant already has {limit} dynamic pools",
            code="pool_limit_reached",
            details={"limit": limit},
        )


async def create_pool(
    ctx: RequestContext, db: AsyncSession, body: PoolCreate, *, template_key: str | None = None
) -> TalentPool:
    principal = common.principal_of(ctx)
    body = body.model_copy(update={"team_id": _check_visibility(principal, body.visibility, body.team_id)})
    segment_id = None
    if body.kind == "dynamic":
        await check_dynamic_limit(ctx, db)
        segment_id = (await _segment_for(ctx, db, principal, body)).id
    elif body.segment_id is not None or body.segment is not None:
        raise ValidationFailed(
            "A static pool has no segment", code="static_pool_segment", details={"path": "segment_id"}
        )
    pool = TalentPool(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        name=body.name,
        description=body.description,
        kind=body.kind,
        segment_id=segment_id,
        owner_id=principal.id,
        visibility=body.visibility,
        team_id=body.team_id if body.visibility == "team" else None,
        status="active",
        template_key=template_key,
        member_count=0,
    )
    db.add(pool)
    await db.flush()
    if pool.kind == "dynamic":
        await refresh.request_refresh(db, pool, "created")
    return pool


async def update_pool(
    ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID, body: PoolUpdate
) -> TalentPool:
    principal = common.principal_of(ctx)
    pool = await common.get_pool(ctx, db, pool_id, for_update=True)
    common.require_edit(principal, pool)
    sent = body.model_fields_set
    if "name" in sent and body.name:
        pool.name = body.name
    if "description" in sent:
        pool.description = body.description
    if "visibility" in sent and body.visibility:
        team = body.team_id if "team_id" in sent else pool.team_id
        team = _check_visibility(principal, body.visibility, team)
        pool.visibility = body.visibility
        pool.team_id = team if body.visibility == "team" else None
    await db.flush()
    if pool.legacy_list_id is not None:
        from app.services.talent_pools import legacy_lists

        await legacy_lists.mirror_pool(db, pool)
    return pool


async def archive_pool(ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID) -> TalentPool:
    principal = common.principal_of(ctx)
    pool = await common.get_pool(ctx, db, pool_id, for_update=True)
    common.require_edit(principal, pool)
    if pool.status != "archived":
        pool.status = "archived"
        pool.archived_at = common.now()
        pool.refresh_due_at = None
        await db.flush()
        if pool.legacy_list_id is not None:
            from app.services.talent_pools import legacy_lists

            await legacy_lists.mirror_pool(db, pool)
    return pool


async def set_subscription(
    ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID, subscribed: bool
) -> SubscriptionOut:
    principal = common.principal_of(ctx)
    pool = await common.get_pool(ctx, db, pool_id)
    existing = (
        await db.execute(
            select(TalentPoolSubscription).where(
                TalentPoolSubscription.pool_id == pool.id, TalentPoolSubscription.user_id == principal.id
            )
        )
    ).scalar_one_or_none()
    if subscribed and existing is None:
        db.add(
            TalentPoolSubscription(
                tenant_id=pool.tenant_id, pool_id=pool.id, user_id=principal.id, last_digest_at=common.now()
            )
        )
    elif not subscribed and existing is not None:
        await db.delete(existing)
    await db.flush()
    return SubscriptionOut(subscribed=subscribed)
