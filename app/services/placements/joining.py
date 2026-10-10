"""Joined / Dropped / joining confirmation / counter-offer / dropout risk (Epic 19 A5-A6, FR-105, FR-106).

* ``mark_joined``: ★ human only; locks the placement, re-runs every resolver and asserts the gates
  (``gates.assert_clear_for_update``), requires ``actual_start_date <= today`` in the Legal Entity's timezone, then
  transitions to ``active`` and emits ``PlacementActivated``.
* ``drop``: transitions to ``dropped`` and emits ``PlacementDropped``; the offer's filled count is decremented
  through ``offers_service.record_placement_dropped``; a Filled demand is **never** reopened automatically -- its
  owner is notified (``placements.demand_reopen_suggested``).
"""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef, actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.core.events import emit
from app.events.placements import PlacementActivated, PlacementDropped
from app.models.placements import Placement, PlacementTouchpoint
from app.services.placements import dropout, gates, status
from app.services.placements.common import PRE_START, get_placement, local_today, now, settings_row
from app.services.placements.events import DemandReopenSuggested, DropoutRiskRaised, JoiningConfirmed


async def mark_joined(
    ctx: RequestContext,
    db: AsyncSession,
    placement_id: uuid.UUID,
    actual_start_date: date,
    *,
    actor: ActorRef | None = None,
) -> Placement:
    actor = actor or actor_from_ctx(ctx)
    p = await get_placement(db, placement_id, lock=True)
    status.check_transition(p.status, "active", actor.kind)
    await gates.assert_clear_for_update(ctx, db, placement_id)
    today = await local_today(db, p)
    if actual_start_date > today:
        raise ValidationFailed(
            "The actual start date cannot be in the future",
            code="start_in_future",
            details={"field": "actual_start_date"},
        )
    p.actual_start_date = actual_start_date
    await status.transition_placement(ctx, db, p, "active", actor, reason="joined")
    emit(
        db,
        PlacementActivated(
            tenant_id=p.tenant_id,
            placement_id=p.id,
            candidate_id=p.candidate_id,
            actual_start_date=actual_start_date,
        ),
    )
    return p


async def drop(
    ctx: RequestContext,
    db: AsyncSession,
    placement_id: uuid.UUID,
    reason: str,
    note: str | None,
    *,
    actor: ActorRef | None = None,
) -> Placement:
    from app.models.demands import Demand
    from app.services import offers_service

    actor = actor or actor_from_ctx(ctx)
    p = await get_placement(db, placement_id, lock=True)
    if not reason:
        raise ValidationFailed("A drop reason is required", code="reason_required")
    p.drop_reason = reason
    p.drop_note = note
    await status.transition_placement(ctx, db, p, "dropped", actor, reason=reason, note=note)
    emit(db, PlacementDropped(tenant_id=p.tenant_id, placement_id=p.id, demand_id=p.demand_id, reason=reason))
    if p.offer_id is not None:
        await offers_service.record_placement_dropped(ctx, db, p.offer_id)
    if p.demand_id is not None:
        demand = (await db.execute(select(Demand).where(Demand.id == p.demand_id))).scalar_one_or_none()
        if demand is not None and demand.status == "filled":
            emit(
                db,
                DemandReopenSuggested(
                    tenant_id=p.tenant_id, placement_id=p.id, demand_id=demand.id, owner_id=demand.owner_id
                ),
            )
    return p


async def confirm_joining(
    ctx: RequestContext, db: AsyncSession, placement: Placement, actor: ActorRef, via: str
) -> Placement:
    if placement.status == "joining_confirmed":
        return placement
    if placement.status != "pre_joining":
        raise ConflictError("This placement can no longer be confirmed", code="transition_not_allowed")
    placement.joining_confirmed_at = now()
    placement.joining_confirmed_via = via
    await status.transition_placement(
        ctx, db, placement, "joining_confirmed", actor, reason="joining_confirmed"
    )
    emit(
        db,
        JoiningConfirmed(
            tenant_id=placement.tenant_id,
            placement_id=placement.id,
            candidate_id=placement.candidate_id,
            via=via,
        ),
    )
    return placement


async def record_counter_offer(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, note: str | None
) -> Placement:
    p = await get_placement(db, placement_id, lock=True)
    if p.status not in PRE_START:
        raise ConflictError("Counter-offers are recorded before joining", code="transition_not_allowed")
    p.counter_offer_at = now()
    p.counter_offer_note = note
    p.lock_version += 1
    await db.flush()
    await evaluate_dropout(ctx, db, p)
    return p


async def clear_dropout(
    ctx: RequestContext,
    db: AsyncSession,
    placement_id: uuid.UUID,
    note: str | None,  # noqa: ARG001
) -> Placement:
    actor = actor_from_ctx(ctx)
    p = await get_placement(db, placement_id, lock=True)
    p.dropout_risk = False
    p.dropout_cleared_at = now()
    p.dropout_cleared_by = actor.id if actor.kind == "user" else None
    p.lock_version += 1
    await db.flush()
    return p


async def evaluate_dropout(ctx: RequestContext, db: AsyncSession, p: Placement) -> list[str]:  # noqa: ARG001
    """Evaluate the rules; raise the flag (and notify the recruiter) only when a *new* rule fires."""
    if p.status not in PRE_START:
        return []
    settings = await settings_row(db, p.tenant_id)
    tps = (
        await db.execute(select(PlacementTouchpoint).where(PlacementTouchpoint.placement_id == p.id))
    ).scalars()
    facts = [
        dropout.TouchpointFact(sent=t.sent_at is not None, responded=t.responded_at is not None) for t in tps
    ]
    today = await local_today(db, p)
    fired = dropout.evaluate(
        dict(settings.dropout_rules or {}),
        facts,
        p.onboarding_pct,
        (p.planned_start_date - today).days,
        p.counter_offer_at,
    )
    new = dropout.newly_fired(list(p.dropout_risk_rules or []), fired)
    if new:
        p.dropout_risk = True
        p.dropout_risk_rules = sorted(set(p.dropout_risk_rules or []) | set(fired))
        await db.flush()
        emit(
            db,
            DropoutRiskRaised(
                tenant_id=p.tenant_id,
                placement_id=p.id,
                recruiter_user_id=p.recruiter_user_id,
                rules=tuple(sorted(new)),
            ),
        )
    return fired


def _unused(*_: Any) -> None:  # pragma: no cover
    return None
