# ruff: noqa: E501, D401
"""Referral rewards (Epic 42 story 42.4, FR-216, AD-24, AD-50).

``pending_condition -> eligible -> approved* -> paid`` or ``void``.

* Created on ``hired`` for a reward-eligible referral with a program: amount = the program's reward snapshot,
  ``condition_due_on`` = the hire's actual start + ``joining_condition_days`` (set once the Placement is active).
* Payout channel: a Worker referrer with an open ``payee_kind='worker'`` Placement -> ``payroll`` (paid in the next
  Payroll Export through the ``referral_reward`` posting source); everyone else -> ``payout_export``.
* The daily evaluator makes due rewards ``eligible`` (system) while the hire's Placement is active / offboarding
  and starts the approval (subject ``referral_reward``); a Placement that ends or drops before the due date voids
  the reward (``condition_not_met`` / ``placement_dropped``).
* Approval is human only (*): the engine accepts only ``APPROVER_KINDS`` and ``mark_approved`` requires a human
  actor. With no active chain the request goes to the Finance role (never auto-approved).
"""

import uuid
from datetime import date, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import ActorRef, require_human
from app.core.context import RequestContext
from app.core.events import emit
from app.models.placements import Placement
from app.models.referrals import Referral, ReferralReward
from app.schemas.referrals import RewardOut
from app.services.referrals import eligibility
from app.services.referrals.common import (
    ACTIVE_PLACEMENT_STATUSES,
    APPROVAL_SUBJECT,
    OPEN_PLACEMENT_STATUSES,
    REF_MANAGE,
    REF_READ,
    conflict,
    not_found,
    now,
    referrer_names,
    require,
    system_ctx,
    today,
)
from app.services.referrals.events import ReferralRewardEligible

VOIDABLE = ("pending_condition", "eligible")
DEFAULT_APPROVER_ROLE = "finance"


async def _payee_placement(db: AsyncSession, candidate_id: uuid.UUID | None) -> uuid.UUID | None:
    """The Worker referrer's own payroll Placement (open, ``payee_kind='worker'``; active first, latest start)."""
    if candidate_id is None:
        return None
    rows = list(
        (
            await db.execute(
                select(Placement)
                .where(
                    Placement.candidate_id == candidate_id,
                    Placement.payee_kind == "worker",
                    Placement.status.in_(OPEN_PLACEMENT_STATUSES),
                )
                .order_by(Placement.planned_start_date.desc(), Placement.id)
            )
        ).scalars()
    )
    rows.sort(key=lambda p: p.status not in ACTIVE_PLACEMENT_STATUSES)
    return rows[0].id if rows else None


async def _set_channel(db: AsyncSession, reward: ReferralReward) -> None:
    payee = (
        await _payee_placement(db, reward.referrer_candidate_id) if reward.referrer_kind == "worker" else None
    )
    reward.payout_channel = "payroll" if payee is not None else "payout_export"
    reward.payee_placement_id = payee


def _due(placement: Placement, days: int) -> date | None:
    if placement.actual_start_date is None or placement.status not in (*ACTIVE_PLACEMENT_STATUSES, "ended"):
        return None
    return placement.actual_start_date + timedelta(days=days)


async def create_for_hire(
    db: AsyncSession, referral: Referral, placement: Placement
) -> ReferralReward | None:
    """One reward per referral (UNIQUE): created ``pending_condition`` with the reward snapshot."""
    existing = (
        await db.execute(select(ReferralReward).where(ReferralReward.referral_id == referral.id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    program = await eligibility.load_program(db, referral.program_id)
    facts = await eligibility.demand_facts(db, referral.demand_id)
    if program is None or facts is None:
        return None
    amount = eligibility.reward_amount(program, facts)
    if amount is None:
        logger.info("referral {}: program {} has no reward for this opening", referral.id, program.id)
        return None
    reward = ReferralReward(
        id=uuid.uuid4(),
        tenant_id=referral.tenant_id,
        referral_id=referral.id,
        program_id=program.id,
        placement_id=placement.id,
        referrer_kind=referral.referrer_kind,
        referrer_user_id=referral.referrer_user_id,
        referrer_candidate_id=referral.referrer_candidate_id,
        amount=amount.amount,
        currency=amount.currency,
        status="pending_condition",
        condition_due_on=_due(placement, program.joining_condition_days),
        payout_channel="payout_export",
    )
    await _set_channel(db, reward)
    db.add(reward)
    await db.flush()
    audit.record(
        "referral.reward_created", "referral_reward", reward.id, session=db, amount=str(reward.amount)
    )
    return reward


async def _for_placement(db: AsyncSession, placement_id: uuid.UUID) -> ReferralReward | None:
    return (
        await db.execute(
            select(ReferralReward).where(ReferralReward.placement_id == placement_id).with_for_update()
        )
    ).scalar_one_or_none()


async def on_activated(db: AsyncSession, placement_id: uuid.UUID) -> None:
    reward = await _for_placement(db, placement_id)
    if reward is None or reward.status != "pending_condition":
        return
    placement = await db.get(Placement, placement_id)
    program = await eligibility.load_program(db, reward.program_id)
    if placement is None or program is None:
        return
    reward.condition_due_on = _due(placement, program.joining_condition_days)
    await db.flush()


async def void(db: AsyncSession, reward: ReferralReward, reason: str) -> None:
    from app.services import approvals

    if reward.status not in VOIDABLE:
        return
    reward.status = "void"
    reward.void_reason = reason
    if reward.approval_request_id is not None:
        await approvals.cancel_open(system_ctx(reward.tenant_id), db, APPROVAL_SUBJECT, reward.id)
    await db.flush()
    audit.record("referral.reward_voided", "referral_reward", reward.id, session=db, reason=reason)


async def on_ended(db: AsyncSession, placement_id: uuid.UUID, end_date: date) -> None:
    """The hire left: before the due date the condition is not met; on / after it the reward becomes eligible."""
    reward = await _for_placement(db, placement_id)
    if reward is None or reward.status != "pending_condition":
        return
    if reward.condition_due_on is not None and end_date >= reward.condition_due_on:
        await make_eligible(db, reward)
    else:
        await void(db, reward, "condition_not_met")


async def on_dropped(db: AsyncSession, placement_id: uuid.UUID) -> None:
    reward = await _for_placement(db, placement_id)
    if reward is not None:
        await void(db, reward, "placement_dropped")


def _default_steps() -> list[Any]:
    from app.services.approvals import StepSpec

    return [StepSpec("Finance approval", "finance_role", {"role_key": DEFAULT_APPROVER_ROLE})]


async def _has_chain(db: AsyncSession) -> bool:
    from app.models.approvals import ApprovalChain

    return (
        await db.execute(
            select(ApprovalChain.id).where(
                ApprovalChain.subject_type == APPROVAL_SUBJECT, ApprovalChain.is_active.is_(True)
            )
        )
    ).first() is not None


async def make_eligible(db: AsyncSession, reward: ReferralReward) -> None:
    """System transition: ``eligible`` + an approval request on the engine (never auto-approved)."""
    from app.services import approvals

    if reward.status != "pending_condition":
        return
    reward.status = "eligible"
    reward.eligible_at = now()
    await _set_channel(db, reward)
    await db.flush()
    ctx = system_ctx(reward.tenant_id)
    result = await approvals.start(
        ctx,
        db,
        APPROVAL_SUBJECT,
        reward.id,
        {"amount": str(reward.amount), "currency": reward.currency, "referrer_kind": reward.referrer_kind},
        steps=None if await _has_chain(db) else _default_steps(),
        initiated_by=ActorRef("system", None),
    )
    reward.approval_request_id = result.request_id
    await db.flush()
    emit(
        db,
        ReferralRewardEligible(
            tenant_id=reward.tenant_id,
            reward_id=reward.id,
            referral_id=reward.referral_id,
            approval_request_id=result.request_id,
        ),
    )


async def evaluate(db: AsyncSession, on: date | None = None) -> int:
    """Daily evaluator: due ``pending_condition`` rewards whose hire is still active / offboarding -> eligible."""
    day = on or today()
    rows = list(
        (
            await db.execute(
                select(ReferralReward)
                .join(Placement, Placement.id == ReferralReward.placement_id)
                .where(
                    ReferralReward.status == "pending_condition",
                    ReferralReward.condition_due_on.is_not(None),
                    ReferralReward.condition_due_on <= day,
                    Placement.status.in_(ACTIVE_PLACEMENT_STATUSES),
                )
                .with_for_update(of=ReferralReward)
            )
        ).scalars()
    )
    for reward in rows:
        await make_eligible(db, reward)
    return len(rows)


# ================================================================ approval subject (AD-24)
async def mark_approved(db: AsyncSession, reward: ReferralReward, actor: ActorRef) -> None:
    """``eligible -> approved`` -- human only (*): ``system`` / ``workflow`` / ``ai`` / ``api_key`` are refused."""
    require_human(actor)
    if reward.status != "eligible":
        raise conflict(
            "transition_not_allowed", "Only an eligible reward can be approved", status=reward.status
        )
    reward.status = "approved"
    reward.approved_by = actor.id
    reward.approved_at = now()
    await db.flush()
    audit.record("referral.reward_approved", "referral_reward", reward.id, session=db)


async def on_final_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:  # noqa: ARG001
    reward = await db.get(ReferralReward, decision.subject_id, with_for_update=True)
    if reward is None:
        return
    if decision.outcome == "approved":
        if decision.actor is None:  # auto-approval never approves a reward (human only)
            return
        await mark_approved(db, reward, decision.actor)
    elif decision.outcome == "rejected":
        await void(db, reward, "rejected")
    else:  # changes requested: stays eligible; a new request is started by the next evaluation of the reward
        reward.approval_request_id = None
        await db.flush()


async def describe(ctx: RequestContext, db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:  # noqa: ARG001
    from app.schemas.approvals import SubjectSummary

    rows = list(
        (
            await db.execute(
                select(ReferralReward, Referral)
                .join(Referral, Referral.id == ReferralReward.referral_id)
                .where(ReferralReward.id.in_(ids))
            )
        ).tuples()
    )
    out: dict[uuid.UUID, Any] = {}
    for reward, referral in rows:
        out[reward.id] = SubjectSummary(
            label=f"{reward.amount} {reward.currency}", title=f"Referral reward {referral.human_id}"
        )
    return out


# ================================================================ manual actions
async def _visible_reward(ctx: RequestContext, db: AsyncSession, reward_id: uuid.UUID) -> ReferralReward:
    from app.services.referrals import tracking

    row = await db.get(ReferralReward, reward_id, with_for_update=True)
    if row is None:
        raise not_found("Referral reward")
    await tracking.visible_referral(ctx, db, row.referral_id)
    return row


async def void_manual(ctx: RequestContext, db: AsyncSession, reward_id: uuid.UUID, reason: str) -> RewardOut:
    require(ctx, REF_MANAGE)
    reward = await _visible_reward(ctx, db, reward_id)
    if reward.status not in VOIDABLE:
        raise conflict(
            "reward_not_voidable", "Only a pending or eligible reward can be voided", status=reward.status
        )
    await void(db, reward, "manual")
    audit.record("referral.reward_void_reason", "referral_reward", reward.id, session=db, note=reason[:500])
    return (await outs(db, [reward]))[0]


async def outs(db: AsyncSession, rows: list[ReferralReward]) -> list[RewardOut]:
    from app.services.referrals.tracking import reward_out

    refs = (
        {
            r.id: r
            for r in (
                await db.execute(select(Referral).where(Referral.id.in_([x.referral_id for x in rows])))
            ).scalars()
        }
        if rows
        else {}
    )
    names = await referrer_names(
        db,
        {r.referrer_user_id for r in rows if r.referrer_user_id},
        {r.referrer_candidate_id for r in rows if r.referrer_candidate_id},
    )
    return [reward_out(r, refs.get(r.referral_id), names) for r in rows]


async def list_rewards(
    ctx: RequestContext, db: AsyncSession, *, status: str | None = None, channel: str | None = None
) -> list[RewardOut]:
    from app.models.demands import Demand
    from app.services.referrals.common import scoped

    require(ctx, REF_READ)
    stmt = (
        select(ReferralReward)
        .join(Referral, Referral.id == ReferralReward.referral_id)
        .outerjoin(Demand, Demand.id == Referral.demand_id)
    )
    stmt = scoped(ctx, stmt)
    if status:
        stmt = stmt.where(ReferralReward.status == status)
    if channel:
        stmt = stmt.where(ReferralReward.payout_channel == channel)
    rows = list((await db.execute(stmt.order_by(ReferralReward.created_at.desc()).limit(1000))).scalars())
    return await outs(db, rows)
