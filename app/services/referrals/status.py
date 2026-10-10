# ruff: noqa: E501
"""Referral status handlers (Epic 42 story 42.3, FR-215), system actor. Called by the after-commit tasks of
``app/tasks/referrals.py`` (subscribed in ``hooks.py``).

* ``ApplicationStageChanged`` on a referral application: a move to ``rejected`` / ``withdrawn`` -> ``not_selected``;
  a reopen of a ``not_selected`` referral -> ``in_process``; any other move keeps ``in_process``.
* ``PlacementCreated`` for the referral's application -> ``hired`` [ASSUMPTION: "Hired" = a Placement exists]:
  sets ``placement_id``, emits ``ReferralHired`` and creates the reward when the referral is reward-eligible.
* ``PlacementActivated`` -> the reward's ``condition_due_on``; ``PlacementEnded`` -> eligible or void;
  ``PlacementDropped`` -> ``not_selected`` and the reward voided (``placement_dropped``).
"""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.events import emit
from app.events.referrals import ReferralHired
from app.models.placements import Placement
from app.models.referrals import Referral
from app.services.referrals import rewards
from app.services.referrals.submit import set_status

NOT_SELECTED_STAGES = frozenset({"rejected", "withdrawn"})
FINAL = frozenset({"hired", "duplicate", "declined", "expired", "withdrawn"})


async def _by_application(db: AsyncSession, application_id: uuid.UUID | None) -> Referral | None:
    if application_id is None:
        return None
    return (
        await db.execute(select(Referral).where(Referral.application_id == application_id).with_for_update())
    ).scalar_one_or_none()


async def on_stage_changed(db: AsyncSession, application_id: uuid.UUID, to_key: str) -> str | None:
    audit.set_actor(db, "system", None)
    r = await _by_application(db, application_id)
    if r is None or r.status in FINAL:
        return None
    if to_key in NOT_SELECTED_STAGES:
        set_status(db, r, "not_selected")
    elif r.status in ("not_selected", "accepted"):
        set_status(db, r, "in_process")
        r.decided_at = None
    await db.flush()
    return r.status


async def on_placement_created(db: AsyncSession, placement_id: uuid.UUID) -> str | None:
    audit.set_actor(db, "system", None)
    p = await db.get(Placement, placement_id)
    if p is None:
        return None
    r = await _by_application(db, p.application_id)
    if r is None or r.status in ("hired", "duplicate", "declined", "expired", "withdrawn"):
        return None
    r.placement_id = p.id
    set_status(db, r, "hired")
    await db.flush()
    emit(
        db,
        ReferralHired(
            tenant_id=r.tenant_id,
            referral_id=r.id,
            candidate_id=p.candidate_id,
            application_id=r.application_id,  # type: ignore[arg-type]
            placement_id=p.id,
            referrer_kind=r.referrer_kind,
            referrer_id=r.referrer_user_id or r.referrer_candidate_id,  # type: ignore[arg-type]
        ),
    )
    if r.reward_eligible and r.program_id is not None:
        await rewards.create_for_hire(db, r, p)
    return r.status


async def on_placement_activated(db: AsyncSession, placement_id: uuid.UUID) -> None:
    audit.set_actor(db, "system", None)
    await rewards.on_activated(db, placement_id)


async def on_placement_ended(db: AsyncSession, placement_id: uuid.UUID, end_date: date) -> None:
    audit.set_actor(db, "system", None)
    await rewards.on_ended(db, placement_id, end_date)


async def on_placement_dropped(db: AsyncSession, placement_id: uuid.UUID) -> None:
    audit.set_actor(db, "system", None)
    r = (
        await db.execute(select(Referral).where(Referral.placement_id == placement_id).with_for_update())
    ).scalar_one_or_none()
    if r is not None and r.status == "hired":
        set_status(db, r, "not_selected")
    await rewards.on_dropped(db, placement_id)
    await db.flush()
