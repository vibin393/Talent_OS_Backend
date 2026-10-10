# ruff: noqa: E501
"""Referrer self-service (Epic 42): internal Users on ``/me/referrals`` and Workers on ``/portal/work/referrals``.

No permission key: any active internal User, any Worker (a Candidate with a Placement); program eligibility is the
gate (P1 self-service pattern, like notifications). Output is the referrer-safe schema only.

Workers: ``list_mine_worker`` reads the referrals in the Worker's org-scoped session (``referrals`` carries worker
org RLS on ``referrer_candidate_id``: another Worker's rows are invisible, a withdraw of them is a 404). Openings,
referring and share links need Demands and programs, which a Worker session cannot read: those run in an internal
tenant session after the principal check, with the referrer fixed to the signed-in Worker.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import Principal
from app.schemas.referrals import ReferIn
from app.schemas.referrals_referrer import ReferrerOpeningOut, ReferrerReferralOut, ReferrerShareLinkOut
from app.services.referrals import submit, tracking
from app.services.referrals.common import user_referrer, worker_referrer


# ================================================================ internal Users (/me/referrals)
async def user_openings(principal: Principal, db: AsyncSession) -> list[ReferrerOpeningOut]:
    return await submit.openings(db, await user_referrer(db, principal))


async def user_list(principal: Principal, db: AsyncSession) -> list[ReferrerReferralOut]:
    return await tracking.list_mine_user(db, await user_referrer(db, principal))


async def user_refer(
    ctx: RequestContext,
    principal: Principal,
    db: AsyncSession,
    data: ReferIn,
    resume: bytes,
    filename: str | None,
) -> ReferrerReferralOut:
    return await submit.refer(ctx, db, await user_referrer(db, principal), data, resume, filename)


async def user_withdraw(
    principal: Principal, db: AsyncSession, referral_id: uuid.UUID
) -> ReferrerReferralOut:
    return await submit.withdraw(db, await user_referrer(db, principal), referral_id)


async def user_share_link(
    principal: Principal, db: AsyncSession, demand_id: uuid.UUID | None
) -> ReferrerShareLinkOut:
    return await submit.share_link(db, await user_referrer(db, principal), demand_id)


# ================================================================ Workers (/portal/work/referrals)
async def _titles(tenant_id: uuid.UUID, demand_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    from sqlalchemy import select

    from app.models.demands import Demand

    if not demand_ids:
        return {}
    async with tenant_session(tenant_id) as db:
        return dict(
            (await db.execute(select(Demand.id, Demand.title).where(Demand.id.in_(demand_ids))))
            .tuples()
            .all()
        )


async def worker_list(principal: Any, worker_db: AsyncSession) -> list[ReferrerReferralOut]:
    async with tenant_session(principal.tenant_id) as db:
        await worker_referrer(db, principal.tenant_id, principal.candidate_id)
    rows = await tracking.mine_worker_rows(worker_db, principal.candidate_id)
    titles = await _titles(principal.tenant_id, {r.demand_id for r in rows if r.demand_id})
    return await tracking.referrer_view(worker_db, rows, titles=titles)


async def worker_openings(principal: Any) -> list[ReferrerOpeningOut]:
    async with tenant_session(principal.tenant_id) as db:
        return await submit.openings(
            db, await worker_referrer(db, principal.tenant_id, principal.candidate_id)
        )


async def worker_refer(
    principal: Any, data: ReferIn, resume: bytes, filename: str | None, ip: str | None
) -> ReferrerReferralOut:
    async with tenant_session(principal.tenant_id) as db:
        audit.set_actor(db, "candidate", principal.candidate_id)
        referrer = await worker_referrer(db, principal.tenant_id, principal.candidate_id)
        ctx = RequestContext(tenant_id=principal.tenant_id, ip=ip)
        return await submit.refer(ctx, db, referrer, data, resume, filename)


async def worker_withdraw(
    principal: Any, worker_db: AsyncSession, referral_id: uuid.UUID
) -> ReferrerReferralOut:
    """Ownership is proven in the Worker's own org-scoped session first (another Worker's referral -> 404)."""
    from app.models.referrals import Referral
    from app.services.referrals.common import not_found

    if await worker_db.get(Referral, referral_id) is None:
        raise not_found()
    async with tenant_session(principal.tenant_id) as db:
        audit.set_actor(db, "candidate", principal.candidate_id)
        referrer = await worker_referrer(db, principal.tenant_id, principal.candidate_id)
        return await submit.withdraw(db, referrer, referral_id)


async def worker_share_link(principal: Any, demand_id: uuid.UUID | None) -> ReferrerShareLinkOut:
    async with tenant_session(principal.tenant_id) as db:
        audit.set_actor(db, "candidate", principal.candidate_id)
        referrer = await worker_referrer(db, principal.tenant_id, principal.candidate_id)
        return await submit.share_link(db, referrer, demand_id)
