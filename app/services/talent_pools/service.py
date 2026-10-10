"""Talent pools contract (Epic 38, filling the Epic 36 stubs, AD-39). Signatures are FINAL
(``tests/contracts/signatures.json``).

``member_ids`` is the read path for every other epic (39 audiences, 40 bulk invites, 43 campus pools): the
membership is materialised tenant-wide by the refresh task, so the **viewer's** data scope is applied here on
every read (a system context -- tasks -- reads unscoped, RLS still on).
"""

import uuid
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.candidates import Candidate
from app.models.talent_pools import TalentPool, TalentPoolMember
from app.schemas.search import QuickHit
from app.schemas.talent_pools import PoolSummary
from app.services.talent_pools import common

__all__ = ["member_ids", "pool_summary", "quick_search"]

MAX_LIMIT = 100_000


async def member_ids(
    ctx: RequestContext,
    db: AsyncSession,
    pool_id: uuid.UUID,
    *,
    change: Literal["current", "added", "removed"] = "current",
    refresh_id: uuid.UUID | None = None,
    limit: int | None = None,
    after: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Candidate ids of the pool, ascending, keyset-paginated by ``after`` (viewer data scope applies)."""
    pool = await common.get_pool(ctx, db, pool_id)
    m = TalentPoolMember
    stmt = (
        select(m.candidate_id)
        .join(Candidate, Candidate.id == m.candidate_id)
        .where(m.pool_id == pool.id, common.member_scope(ctx))
    )
    if change == "current":
        stmt = stmt.where(m.removed_at.is_(None))
    else:
        if refresh_id is None:
            raise ValidationFailed(
                "refresh_id is required for added / removed members",
                code="refresh_required",
                details={"path": "refresh_id"},
            )
        column = m.added_refresh_id if change == "added" else m.removed_refresh_id
        stmt = stmt.where(column == refresh_id)
    if after is not None:
        stmt = stmt.where(m.candidate_id > after)
    stmt = stmt.distinct().order_by(m.candidate_id)
    if limit is not None:
        stmt = stmt.limit(max(0, min(int(limit), MAX_LIMIT)))
    return list((await db.execute(stmt)).scalars().all())


async def pool_summary(ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID) -> PoolSummary:
    pool = await common.get_pool(ctx, db, pool_id)
    return PoolSummary(
        id=pool.id,
        name=pool.name,
        kind=pool.kind,
        segment_id=pool.segment_id,
        owner_id=pool.owner_id,
        visibility=pool.visibility,
        status=pool.status,
        member_count=pool.member_count,
        last_refreshed_at=pool.last_refreshed_at,
    )


async def quick_search(ctx: RequestContext, db: AsyncSession, q: str, limit: int) -> list[QuickHit]:
    """Global search group ``talent_pool`` (Epic 52 consumer): visible, active Pools by name."""
    term = (q or "").strip()
    if not term or ctx.principal is None or not common.principal_of(ctx).has(common.READ):
        return []
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = (
        await db.execute(
            select(TalentPool)
            .where(
                common.visible(common.principal_of(ctx)),
                TalentPool.status == "active",
                TalentPool.name.ilike(f"%{escaped}%", escape="\\"),
            )
            .order_by(TalentPool.name)
            .limit(max(1, min(limit, 50)))
        )
    ).scalars()
    return [
        QuickHit(
            id=p.id,
            type="talent_pool",
            title=p.name,
            subtitle=f"{p.kind.capitalize()} pool - {p.member_count} members",
            url=f"/talent-pools/{p.id}",
            exact=p.name.lower() == term.lower(),
        )
        for p in rows
    ]
