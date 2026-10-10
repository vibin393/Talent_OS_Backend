# ruff: noqa: E501
"""Rediscovery with Pools, pins and add-to-pipeline (Epic 38 story 38.5, FR-194, FR-195).

``pools_section`` is registered as the ``pools`` section of ``matching.rediscovery`` (from ``hooks.py``): the
Pools visible to the viewer whose active members are among the Demand's non-dismissed ``matches`` rows (the
top 500 P1 keeps), each with its top 20 by score -- one statement with
``row_number() OVER (PARTITION BY pool_id ORDER BY overall DESC)``. Pinned Pools always come first, even at
zero. Members are filtered by the viewer's candidate scope.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.permissions import forbidden
from app.models.candidates import Candidate
from app.models.matching import Match
from app.models.talent_pools import DemandPoolPin, TalentPool, TalentPoolMember
from app.schemas.pipeline import ApplicationsCreate, ApplicationsCreateOut
from app.schemas.talent_pools import (
    AddToPipelineIn,
    PoolPinOut,
    PoolsSectionOut,
    RediscoveryMemberOut,
    RediscoveryPoolOut,
)
from app.services.talent_pools import common

TOP = 20
MAX_POOLS = 50


async def _demand(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> Any:
    from app.services.demands import matching_port

    return await matching_port.get_demand(ctx, db, demand_id)


async def _pinned(db: AsyncSession, demand_id: uuid.UUID) -> dict[uuid.UUID, DemandPoolPin]:
    rows = (await db.execute(select(DemandPoolPin).where(DemandPoolPin.demand_id == demand_id))).scalars()
    return {p.pool_id: p for p in rows}


async def pools_section(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> dict[str, Any] | None:
    principal = ctx.principal
    if principal is None or not principal.has(common.READ):
        return None
    pinned = await _pinned(db, demand_id)
    m, x = TalentPoolMember, Match
    ranked = (
        select(
            m.pool_id.label("pool_id"),
            x.candidate_id.label("candidate_id"),
            x.overall.label("overall"),
            Candidate.full_name.label("full_name"),
            func.row_number()
            .over(partition_by=m.pool_id, order_by=(x.overall.desc(), x.candidate_id))
            .label("rank"),
            func.count().over(partition_by=m.pool_id).label("matching"),
        )
        .select_from(m)
        .join(x, (x.candidate_id == m.candidate_id) & (x.demand_id == demand_id) & x.dismissed_at.is_(None))
        .join(Candidate, Candidate.id == m.candidate_id)
        .join(TalentPool, TalentPool.id == m.pool_id)
        .where(
            m.removed_at.is_(None),
            TalentPool.status == "active",
            common.visible(common.principal_of(ctx)),
            common.member_scope(ctx),
        )
        .subquery("ranked")
    )
    rows = (
        await db.execute(select(ranked).where(ranked.c.rank <= TOP).order_by(ranked.c.pool_id, ranked.c.rank))
    ).all()
    by_pool: dict[uuid.UUID, dict[str, Any]] = {}
    for r in rows:
        entry = by_pool.setdefault(r.pool_id, {"matching": int(r.matching), "top": []})
        entry["top"].append(
            RediscoveryMemberOut(
                candidate_id=r.candidate_id, full_name=r.full_name, overall=r.overall, rank=int(r.rank)
            )
        )
    pool_ids = set(by_pool) | set(pinned)
    if not pool_ids:
        return PoolsSectionOut(matching_pools=0, summary="0 matching Pools", pools=[]).model_dump(mode="json")
    pools = {
        p.id: p
        for p in (
            await db.execute(
                select(TalentPool).where(
                    TalentPool.id.in_(pool_ids),
                    common.visible(common.principal_of(ctx)),
                    TalentPool.status == "active",
                )
            )
        ).scalars()
    }
    items = [
        RediscoveryPoolOut(
            pool_id=pid,
            name=pool.name,
            kind=pool.kind,
            pinned=pid in pinned,
            matching_count=by_pool.get(pid, {}).get("matching", 0),
            top=by_pool.get(pid, {}).get("top", []),
        )
        for pid, pool in pools.items()
    ]
    items.sort(key=lambda p: (not p.pinned, -p.matching_count, p.name.lower()))
    shown = [p for p in items if p.pinned] + [p for p in items if not p.pinned][:MAX_POOLS]
    n = sum(1 for p in items if p.matching_count > 0)
    summary = f"{n} matching Pool" + ("" if n == 1 else "s")
    return PoolsSectionOut(matching_pools=n, summary=summary, pools=shown).model_dump(mode="json")


# ---------------------------------------------------------------- pins
def _pin_out(pin: DemandPoolPin, pool: TalentPool) -> PoolPinOut:
    return PoolPinOut(
        pool_id=pool.id,
        name=pool.name,
        kind=pool.kind,
        member_count=pool.member_count,
        pinned_by=pin.pinned_by,
        pinned_at=pin.pinned_at,
    )


async def list_pins(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> list[PoolPinOut]:
    await _demand(ctx, db, demand_id)
    principal = common.principal_of(ctx)
    rows = (
        await db.execute(
            select(DemandPoolPin, TalentPool)
            .join(TalentPool, TalentPool.id == DemandPoolPin.pool_id)
            .where(DemandPoolPin.demand_id == demand_id, common.visible(principal))
            .order_by(DemandPoolPin.pinned_at)
        )
    ).all()
    return [_pin_out(pin, pool) for pin, pool in rows]


async def pin(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, pool_id: uuid.UUID) -> PoolPinOut:
    demand = await _demand(ctx, db, demand_id)
    principal = common.principal_of(ctx)
    if not principal.has(common.WRITE):
        raise forbidden(common.WRITE)
    pool = await common.get_pool(ctx, db, pool_id)
    common.require_active(pool)
    existing = (
        await db.execute(
            select(DemandPoolPin).where(
                DemandPoolPin.demand_id == demand.id, DemandPoolPin.pool_id == pool.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _pin_out(existing, pool)
    row = DemandPoolPin(
        tenant_id=pool.tenant_id, demand_id=demand.id, pool_id=pool.id, pinned_by=principal.id
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:  # concurrent pin
        raise ConflictError("This pool is already pinned", code="pool_pinned") from exc
    await db.refresh(row)
    return _pin_out(row, pool)


async def unpin(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, pool_id: uuid.UUID) -> None:
    await _demand(ctx, db, demand_id)
    principal = common.principal_of(ctx)
    if not principal.has(common.WRITE):
        raise forbidden(common.WRITE)
    row = (
        await db.execute(
            select(DemandPoolPin).where(
                DemandPoolPin.demand_id == demand_id, DemandPoolPin.pool_id == pool_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Pin not found")
    await common.get_pool(ctx, db, pool_id)
    await db.delete(row)
    await db.flush()


# ---------------------------------------------------------------- add to pipeline
async def add_to_pipeline(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, pool_id: uuid.UUID, body: AddToPipelineIn
) -> ApplicationsCreateOut:
    """P1 ``pipeline.add_candidates`` with ``source='saved_list'``, ``source_ref='pool:<id>'`` (no new source)."""
    from app.services import pipeline_service

    pool = await common.get_pool(ctx, db, pool_id)
    m = TalentPoolMember
    active = (
        select(m.candidate_id)
        .join(Candidate, Candidate.id == m.candidate_id)
        .where(m.pool_id == pool.id, m.removed_at.is_(None), common.member_scope(ctx))
    )
    if body.candidate_ids:
        wanted = list(dict.fromkeys(body.candidate_ids))
        found = set((await db.execute(active.where(m.candidate_id.in_(wanted)))).scalars())
        missing = [c for c in wanted if c not in found]
        if missing:
            raise NotFound("Candidate is not in this pool", details={"candidate_id": str(missing[0])})
        ids = wanted
    elif body.top_n:
        await _demand(ctx, db, demand_id)
        ids = list(
            (
                await db.execute(
                    active.join(
                        Match,
                        (Match.candidate_id == m.candidate_id)
                        & (Match.demand_id == demand_id)
                        & Match.dismissed_at.is_(None),
                    )
                    .order_by(Match.overall.desc(), m.candidate_id)
                    .limit(body.top_n)
                )
            ).scalars()
        )
        if not ids:
            raise ConflictError("No pool member matches this demand", code="no_matching_members")
    else:
        raise ValidationFailed(
            "Send candidate_ids or top_n", code="candidates_required", details={"path": "candidate_ids"}
        )
    return await pipeline_service.add_candidates(
        ctx,
        db,
        demand_id,
        ApplicationsCreate(candidate_ids=ids, source="saved_list", source_ref=f"pool:{pool.id}"),
    )
