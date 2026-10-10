# ruff: noqa: E501, D401
"""Referral tracking (Epic 42 story 42.3, FR-215).

* Referrer-safe views (``ReferrerReferralOut``): four status labels and the referrer's own reward only.
  ``list_mine_user`` (``/me/referrals``) and ``list_mine_worker`` (``/portal/work/referrals``; the rows come from
  the Worker's org-scoped session, so another Worker's referrals are invisible -> 404).
* Internal views (``referral:read``, data-scoped through the Demand's owner / BU; general referrals need tenant
  scope): the full record, the reward and Candidate 360's "Referred by".
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.referrals import Referral, ReferralProgram, ReferralReward
from app.schemas.referrals import CandidateReferralOut, ReferralOut, ReferralPage, RewardOut
from app.schemas.referrals_referrer import REWARD_LABELS, ReferrerReferralOut, ReferrerRewardOut, status_label
from app.services.referrals.common import (
    REF_READ,
    Referrer,
    not_found,
    referrer_names,
    require,
    scoped,
    today,
)

__all__ = ["today"]


def owned_by(referral: Referral, referrer: Referrer) -> bool:
    if referrer.kind == "user":
        return referral.referrer_kind == "user" and referral.referrer_user_id == referrer.id
    return referral.referrer_kind == "worker" and referral.referrer_candidate_id == referrer.id


async def referrer_of(db: AsyncSession, referral: Referral) -> Referrer:
    from app.models.candidates import Candidate
    from app.models.identity import Role, User, UserRole

    if referral.referrer_kind == "user":
        u = await db.get(User, referral.referrer_user_id)
        keys = frozenset(
            (
                await db.execute(
                    select(Role.key)
                    .join(UserRole, UserRole.role_id == Role.id)
                    .where(UserRole.user_id == referral.referrer_user_id)
                )
            ).scalars()
        )
        assert referral.referrer_user_id is not None
        return Referrer(
            "user",
            referral.referrer_user_id,
            referral.tenant_id,
            str(u.email) if u else None,
            u.name if u else None,
            keys,
        )
    assert referral.referrer_candidate_id is not None
    c = await db.get(Candidate, referral.referrer_candidate_id)
    return Referrer(
        "worker", referral.referrer_candidate_id, referral.tenant_id, None, c.full_name if c else None
    )


async def _titles(db: AsyncSession, demand_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    from app.models.demands import Demand

    if not demand_ids:
        return {}
    return dict(
        (await db.execute(select(Demand.id, Demand.title).where(Demand.id.in_(demand_ids)))).tuples().all()
    )


async def _rewards(db: AsyncSession, referral_ids: list[uuid.UUID]) -> dict[uuid.UUID, ReferralReward]:
    if not referral_ids:
        return {}
    rows = (
        await db.execute(select(ReferralReward).where(ReferralReward.referral_id.in_(referral_ids)))
    ).scalars()
    return {r.referral_id: r for r in rows}


def _referrer_reward(row: ReferralReward | None) -> ReferrerRewardOut | None:
    if row is None:
        return None
    show = row.status != "void"
    return ReferrerRewardOut(
        status_label=REWARD_LABELS[row.status],
        amount=row.amount if show else None,
        currency=row.currency if show else None,
    )


async def referrer_view(
    db: AsyncSession, referrals: list[Referral], *, titles: dict[uuid.UUID, str] | None = None
) -> list[ReferrerReferralOut]:
    """The referrer-safe projection. ``titles`` lets the Worker path pass Demand titles read outside its
    org-scoped session (Demands are not visible to a Worker session)."""
    if titles is None:
        titles = await _titles(db, {r.demand_id for r in referrals if r.demand_id})
    rewards = await _rewards(db, [r.id for r in referrals])
    return [
        ReferrerReferralOut(
            id=r.id,
            human_id=r.human_id,
            referee_name=r.referee_name,
            demand_title=titles.get(r.demand_id) if r.demand_id else None,
            submitted_at=r.created_at,
            status_label=status_label(r.status),
            can_withdraw=r.status == "submitted",
            reward=_referrer_reward(rewards.get(r.id)),
        )
        for r in referrals
    ]


async def list_mine_user(db: AsyncSession, referrer: Referrer) -> list[ReferrerReferralOut]:
    rows = list(
        (
            await db.execute(
                select(Referral)
                .where(Referral.referrer_kind == "user", Referral.referrer_user_id == referrer.id)
                .order_by(Referral.created_at.desc())
            )
        ).scalars()
    )
    return await referrer_view(db, rows)


async def mine_worker_rows(db: AsyncSession, candidate_id: uuid.UUID) -> list[Referral]:
    """The Worker's referrals, read in the Worker's org-scoped session (RLS)."""
    return list(
        (
            await db.execute(
                select(Referral)
                .where(Referral.referrer_kind == "worker", Referral.referrer_candidate_id == candidate_id)
                .order_by(Referral.created_at.desc())
            )
        ).scalars()
    )


# ================================================================ internal
def _base() -> Any:
    from app.models.demands import Demand

    return select(Referral).outerjoin(Demand, Demand.id == Referral.demand_id)


async def visible_referral(
    ctx: RequestContext, db: AsyncSession, referral_id: uuid.UUID, *, for_update: bool = False
) -> Referral:
    require(ctx, REF_READ)
    stmt = scoped(ctx, _base().where(Referral.id == referral_id))
    if for_update:
        stmt = stmt.with_for_update(of=Referral)
    row: Referral | None = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise not_found()
    return row


def reward_out(row: ReferralReward, referral: Referral | None, names: dict[uuid.UUID, str]) -> RewardOut:
    rid = row.referrer_user_id or row.referrer_candidate_id
    return RewardOut(
        id=row.id,
        referral_id=row.referral_id,
        referral_human_id=referral.human_id if referral else "",
        referee_name=referral.referee_name if referral else None,
        referrer_kind=row.referrer_kind,
        referrer_name=names.get(rid) if rid else None,
        program_id=row.program_id,
        placement_id=row.placement_id,
        status=row.status,  # type: ignore[arg-type]
        amount=row.amount,
        currency=row.currency,
        condition_due_on=row.condition_due_on,
        eligible_at=row.eligible_at,
        approval_request_id=row.approval_request_id,
        approved_at=row.approved_at,
        payout_channel=row.payout_channel,
        payee_placement_id=row.payee_placement_id,
        paid_at=row.paid_at,
        payout_ref=row.payout_ref,
        void_reason=row.void_reason,
        created_at=row.created_at,
    )


async def _internal_many(db: AsyncSession, rows: list[Referral]) -> list[ReferralOut]:
    titles = await _titles(db, {r.demand_id for r in rows if r.demand_id})
    names = await referrer_names(
        db,
        {r.referrer_user_id for r in rows if r.referrer_user_id},
        {r.referrer_candidate_id for r in rows if r.referrer_candidate_id},
    )
    pids = {r.program_id for r in rows if r.program_id}
    programs = (
        dict(
            (
                await db.execute(
                    select(ReferralProgram.id, ReferralProgram.name).where(ReferralProgram.id.in_(pids))
                )
            )
            .tuples()
            .all()
        )
        if pids
        else {}
    )
    rewards = await _rewards(db, [r.id for r in rows])
    out: list[ReferralOut] = []
    for r in rows:
        rid = r.referrer_user_id or r.referrer_candidate_id
        rw = rewards.get(r.id)
        out.append(
            ReferralOut(
                id=r.id,
                human_id=r.human_id,
                status=r.status,  # type: ignore[arg-type]
                channel=r.channel,
                demand_id=r.demand_id,
                demand_title=titles.get(r.demand_id) if r.demand_id else None,
                program_id=r.program_id,
                program_name=programs.get(r.program_id) if r.program_id else None,
                referrer_kind=r.referrer_kind,
                referrer_user_id=r.referrer_user_id,
                referrer_candidate_id=r.referrer_candidate_id,
                referrer_name=names.get(rid) if rid else None,
                referee_name=r.referee_name,
                referee_email=str(r.referee_email) if r.referee_email else None,
                referee_phone=r.referee_phone,
                relationship=r.relationship,
                note=r.note,
                has_resume=r.resume_file_id is not None,
                candidate_id=r.candidate_id,
                application_id=r.application_id,
                placement_id=r.placement_id,
                reward_eligible=r.reward_eligible,
                ineligible_reason=r.ineligible_reason,
                accepted_at=r.accepted_at,
                decided_at=r.decided_at,
                created_at=r.created_at,
                reward=reward_out(rw, r, names) if rw is not None else None,
            )
        )
    return out


async def internal_out(db: AsyncSession, referral: Referral) -> ReferralOut:
    return (await _internal_many(db, [referral]))[0]


async def list_referrals(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    status: str | None = None,
    demand_id: uuid.UUID | None = None,
    program_id: uuid.UUID | None = None,
    channel: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> ReferralPage:
    require(ctx, REF_READ)
    stmt = scoped(ctx, _base())
    if status:
        stmt = stmt.where(Referral.status == status)
    if demand_id:
        stmt = stmt.where(Referral.demand_id == demand_id)
    if program_id:
        stmt = stmt.where(Referral.program_id == program_id)
    if channel:
        stmt = stmt.where(Referral.channel == channel)
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = list(
        (
            await db.execute(
                stmt.order_by(Referral.created_at.desc(), Referral.id).limit(limit).offset(offset)
            )
        ).scalars()
    )
    return ReferralPage(items=await _internal_many(db, rows), total=total)


async def get_referral(ctx: RequestContext, db: AsyncSession, referral_id: uuid.UUID) -> ReferralOut:
    return await internal_out(db, await visible_referral(ctx, db, referral_id))


async def for_candidate(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID
) -> list[CandidateReferralOut]:
    """Candidate 360 "Referred by" (the referrals that produced or matched this Candidate)."""
    require(ctx, REF_READ)
    rows = list(
        (
            await db.execute(
                scoped(ctx, _base().where(Referral.candidate_id == candidate_id)).order_by(
                    Referral.created_at.desc()
                )
            )
        ).scalars()
    )
    names = await referrer_names(
        db,
        {r.referrer_user_id for r in rows if r.referrer_user_id},
        {r.referrer_candidate_id for r in rows if r.referrer_candidate_id},
    )
    return [
        CandidateReferralOut(
            referral_id=r.id,
            human_id=r.human_id,
            referrer_name=names.get(r.referrer_user_id or r.referrer_candidate_id),  # type: ignore[arg-type]
            referrer_kind=r.referrer_kind,
            status=r.status,  # type: ignore[arg-type]
            created_at=r.created_at,
        )
        for r in rows
    ]


async def resume_download(ctx: RequestContext, db: AsyncSession, referral_id: uuid.UUID) -> Any:
    """The referral's resume (``referral:read`` in scope), via the P1 file download."""
    from app.services import files_service

    r = await visible_referral(ctx, db, referral_id)
    if r.resume_file_id is None:
        raise not_found("Resume")
    file = await files_service.get_file(db, r.resume_file_id)
    if file is None or file.deleted_at is not None:
        raise not_found("Resume")
    return file
