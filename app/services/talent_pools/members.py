# ruff: noqa: E501
"""Static Pool members and the members page (Epic 38 story 38.2, FR-192).

* Manual adds / removals are for static Pools only (409 ``pool_is_dynamic``): dynamic membership is
  segment-only so the refresh diff stays exact.
* **Mirror writes (decision 11):** a Pool with ``legacy_list_id`` writes the same change to the P1
  ``candidate_list_members`` table in the same transaction (direction ``talent_pools -> candidates``), so P1 reads
  (list filter, Candidate 360 "Lists") keep working unchanged.
* The members page is filtered by the viewer's ``candidate:read`` scope; consent is displayed only (AD-42).
"""

import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.models.candidates import Candidate, CandidateListMember
from app.models.privacy import Consent
from app.models.talent_pools import TalentPool, TalentPoolMember
from app.schemas.talent_pools import PoolMemberOut, PoolMembersAddOut, PoolMembersPage
from app.services.talent_pools import common

CONSENT_PURPOSES = ("marketing", "engagement")


def require_static(pool: TalentPool) -> None:
    if pool.kind != "static":
        raise ConflictError("Members of a dynamic pool come from its segment", code="pool_is_dynamic")


async def visible_candidate_ids(
    ctx: RequestContext, db: AsyncSession, ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    if not ids:
        return set()
    rows = await db.execute(select(Candidate.id).where(Candidate.id.in_(ids), common.member_scope(ctx)))
    return set(rows.scalars())


async def _sync_count(db: AsyncSession, pool: TalentPool) -> None:
    pool.member_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(TalentPoolMember)
                .where(TalentPoolMember.pool_id == pool.id, TalentPoolMember.removed_at.is_(None))
            )
        ).scalar_one()
    )


async def add_members(
    ctx: RequestContext,
    db: AsyncSession,
    pool: TalentPool,
    candidate_ids: list[uuid.UUID],
    *,
    source: str = "manual",
    actor_id: uuid.UUID | None = None,
    note: str | None = None,
    allow_archived: bool = False,
) -> PoolMembersAddOut:
    require_static(pool)
    if not allow_archived:
        common.require_active(pool)
    ids = list(dict.fromkeys(candidate_ids))
    allowed = await visible_candidate_ids(ctx, db, ids)
    missing = [i for i in ids if i not in allowed]
    if missing:
        raise NotFound("Candidate not found", details={"candidate_id": str(missing[0])})
    m = TalentPoolMember
    have = set(
        (
            await db.execute(
                select(m.candidate_id).where(
                    m.pool_id == pool.id, m.removed_at.is_(None), m.candidate_id.in_(ids)
                )
            )
        ).scalars()
    )
    new = [i for i in ids if i not in have]
    reason: dict[str, Any] = {"added_by": str(actor_id) if actor_id else None}
    if note:
        reason["note"] = note[:500]
    at = common.now()
    for cid in new:
        db.add(
            TalentPoolMember(
                tenant_id=pool.tenant_id,
                pool_id=pool.id,
                candidate_id=cid,
                source=source,
                reason=reason,
                added_by=actor_id,
                added_at=at,
            )
        )
    await db.flush()
    if pool.legacy_list_id is not None and new:
        await db.execute(
            pg_insert(CandidateListMember)
            .values(
                [
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": pool.tenant_id,
                        "list_id": pool.legacy_list_id,
                        "candidate_id": cid,
                        "added_by": actor_id,
                    }
                    for cid in new
                ]
            )
            .on_conflict_do_nothing(index_elements=["list_id", "candidate_id"])
        )
    await _sync_count(db, pool)
    await db.flush()
    return PoolMembersAddOut(added=len(new), existing=len(ids) - len(new), member_count=pool.member_count)


async def remove_member(
    ctx: RequestContext,
    db: AsyncSession,
    pool: TalentPool,
    candidate_id: uuid.UUID,
    *,
    allow_archived: bool = False,
) -> None:
    require_static(pool)
    if not allow_archived:
        common.require_active(pool)
    member = (
        await db.execute(
            select(TalentPoolMember).where(
                TalentPoolMember.pool_id == pool.id,
                TalentPoolMember.candidate_id == candidate_id,
                TalentPoolMember.removed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if member is None or not await visible_candidate_ids(ctx, db, [candidate_id]):
        raise NotFound("Candidate is not in this pool")
    member.removed_at = common.now()
    if pool.legacy_list_id is not None:
        await db.execute(
            delete(CandidateListMember).where(
                CandidateListMember.list_id == pool.legacy_list_id,
                CandidateListMember.candidate_id == candidate_id,
            )
        )
    await db.flush()
    await _sync_count(db, pool)
    await db.flush()


async def _consents(db: AsyncSession, ids: list[uuid.UUID]) -> dict[tuple[uuid.UUID, str], str]:
    if not ids:
        return {}
    rows = await db.execute(
        select(Consent.candidate_id, Consent.purpose, Consent.status)
        .where(
            Consent.candidate_id.in_(ids), Consent.purpose.in_(CONSENT_PURPOSES), Consent.client_id.is_(None)
        )
        .order_by(Consent.updated_at.asc())
    )
    out: dict[tuple[uuid.UUID, str], str] = {}
    for cid, purpose, status in rows.all():
        out[cid, purpose] = status if status in ("granted", "withdrawn") else "none"
    return out


async def list_members(
    ctx: RequestContext, db: AsyncSession, pool: TalentPool, *, page: int, page_size: int
) -> PoolMembersPage:
    m = TalentPoolMember
    base = (
        select(m, Candidate.full_name, Candidate.current_title)
        .join(Candidate, Candidate.id == m.candidate_id)
        .where(m.pool_id == pool.id, m.removed_at.is_(None), common.member_scope(ctx))
    )
    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
    rows = (
        await db.execute(
            base.order_by(m.added_at.desc(), m.candidate_id).limit(page_size).offset((page - 1) * page_size)
        )
    ).all()
    consents = await _consents(db, [r[0].candidate_id for r in rows])
    items = [
        PoolMemberOut(
            candidate_id=row.candidate_id,
            full_name=name,
            current_title=title,
            source=row.source,
            reason=dict(row.reason or {}),
            added_at=row.added_at,
            added_by=row.added_by,
            consent_marketing=consents.get((row.candidate_id, "marketing"), "none"),
            consent_engagement=consents.get((row.candidate_id, "engagement"), "none"),
        )
        for row, name, title in rows
    ]
    return PoolMembersPage(items=items, total=total, page=page, page_size=page_size)
