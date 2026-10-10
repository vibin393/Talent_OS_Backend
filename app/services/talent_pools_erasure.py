# ruff: noqa: E501
"""Talent pool erasure handler ``talent_pools.members`` (40), Epic 38 A10 (FR-258, AD-34).

On Candidate erasure the subject's ``talent_pool_members`` rows (active and historical) are deleted and the
affected Pools' ``member_count`` is recomputed. ``export`` lists the Pools the subject belongs to (names only).
Segments, versions, shares, refreshes, subscriptions and pins hold no person data (definitions are refused when
they name a candidate id).
"""

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.talent_pools import TalentPool, TalentPoolMember
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class TalentPoolMembersHandler:
    name = "talent_pools.members"
    order = 40
    tables = ("talent_pool_members",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        pool_ids = set(
            (
                await db.execute(
                    select(TalentPoolMember.pool_id).where(TalentPoolMember.candidate_id.in_(ids))
                )
            ).scalars()
        )
        result = await db.execute(delete(TalentPoolMember).where(TalentPoolMember.candidate_id.in_(ids)))
        await db.flush()
        for pool in (await db.execute(select(TalentPool).where(TalentPool.id.in_(pool_ids)))).scalars():
            pool.member_count = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(TalentPoolMember)
                        .where(TalentPoolMember.pool_id == pool.id, TalentPoolMember.removed_at.is_(None))
                    )
                ).scalar_one()
            )
        await db.flush()
        return {"members": int(getattr(result, "rowcount", 0) or 0), "pools": len(pool_ids)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = (
            await db.execute(
                select(TalentPool.name, TalentPoolMember.added_at, TalentPoolMember.removed_at)
                .join(TalentPool, TalentPool.id == TalentPoolMember.pool_id)
                .where(TalentPoolMember.candidate_id == ctx.candidate_id)
                .order_by(TalentPoolMember.added_at)
            )
        ).all()
        if not rows:
            return None
        return {
            "talent_pools": [
                {"pool": name, "added_at": added, "removed_at": removed} for name, added, removed in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        left = (
            await db.execute(
                select(TalentPoolMember.id).where(TalentPoolMember.candidate_id.in_(ids)).limit(1)
            )
        ).first()
        return ["talent pool memberships remain"] if left is not None else []


register_erasure_handler(TalentPoolMembersHandler())
