# ruff: noqa: E501
"""Campus Celery tasks (Epic 43). Queue ``maintenance`` (AD-54). Every task starts with ``tenant_id`` (except the
beat fan-out).

* ``campus.evaluate_eligibility(tenant_id, drive_id, pending_only)`` -- after a registration (pending only) or a rule
  change (every evaluable registration), batches of 500 (``services/campus/eligibility.py``).
* ``campus.assessment_event`` / ``campus.offer_event`` / ``campus.placement_event`` / ``campus.interviewed`` --
  after-commit handlers subscribed in ``services/campus/hooks.py``.
* ``campus.tick_all`` (beat, hourly) -> ``campus.tick(tenant_id)``: expire import invites (minimised) and the daily
  ``campus_registration_received`` digest per drive owner.
"""

import uuid

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "maintenance"


async def _evaluate(tid: uuid.UUID, drive_id: uuid.UUID, pending_only: bool) -> dict[str, int]:
    from app.core import audit
    from app.services.campus import eligibility

    async with tenant_session(tid) as db:
        audit.set_actor(db, "system", None)
        return await eligibility.evaluate_drive(db, drive_id, pending_only=pending_only)


@celery_app.task(name="campus.evaluate_eligibility", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def evaluate_eligibility(tenant_id: str, drive_id: str, pending_only: bool = True) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))
    return run(lambda: _evaluate(tid, uuid.UUID(str(drive_id)), bool(pending_only)), tenant_id=tid)  # type: ignore[no-any-return]


async def _assessment(tid: uuid.UUID, invitation_id: uuid.UUID, kind: str, value: str | None) -> bool:
    from app.core import audit
    from app.services.campus import assessments

    async with tenant_session(tid) as db:
        audit.set_actor(db, "system", None)
        if kind == "scored":
            return await assessments.on_scored(db, invitation_id, value)
        return await assessments.on_decided(db, invitation_id, value or "")


@celery_app.task(name="campus.assessment_event", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def assessment_event(tenant_id: str, invitation_id: str, kind: str, value: str | None) -> bool:
    tid = uuid.UUID(str(tenant_id))
    return bool(run(lambda: _assessment(tid, uuid.UUID(str(invitation_id)), kind, value), tenant_id=tid))


async def _offer(tid: uuid.UUID, offer_id: uuid.UUID, kind: str) -> bool:
    from app.core import audit
    from app.services.campus import offers

    async with tenant_session(tid) as db:
        audit.set_actor(db, "system", None)
        if kind == "accepted":
            return await offers.on_offer_accepted(db, offer_id)
        return await offers.on_offer_closed(db, offer_id)


@celery_app.task(name="campus.offer_event", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def offer_event(tenant_id: str, offer_id: str, kind: str) -> bool:
    tid = uuid.UUID(str(tenant_id))
    return bool(run(lambda: _offer(tid, uuid.UUID(str(offer_id)), kind), tenant_id=tid))


async def _placement(tid: uuid.UUID, placement_id: uuid.UUID, kind: str) -> bool:
    from app.core import audit
    from app.services.campus import offers

    async with tenant_session(tid) as db:
        audit.set_actor(db, "system", None)
        if kind == "activated":
            return await offers.on_placement_activated(db, placement_id)
        return await offers.on_placement_dropped(db, placement_id)


@celery_app.task(name="campus.placement_event", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def placement_event(tenant_id: str, placement_id: str, kind: str) -> bool:
    tid = uuid.UUID(str(tenant_id))
    return bool(run(lambda: _placement(tid, uuid.UUID(str(placement_id)), kind), tenant_id=tid))


async def _interviewed(tid: uuid.UUID, application_id: uuid.UUID) -> bool:
    from app.core import audit
    from app.services.campus import offers

    async with tenant_session(tid) as db:
        audit.set_actor(db, "system", None)
        return await offers.on_interviewed(db, application_id)


@celery_app.task(name="campus.interviewed", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def interviewed(tenant_id: str, application_id: str) -> bool:
    tid = uuid.UUID(str(tenant_id))
    return bool(run(lambda: _interviewed(tid, uuid.UUID(str(application_id))), tenant_id=tid))


async def tick_tenant(tid: uuid.UUID) -> dict[str, int]:
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.core import audit
    from app.core.events import emit
    from app.models.campus import CampusDrive, Registration
    from app.services.campus import imports
    from app.services.campus.common import now
    from app.services.campus.domain_events import CampusRegistrationsDigest

    async with tenant_session(tid) as db:
        audit.set_actor(db, "system", None)
        expired = await imports.expire_invites(db)
        at = now()
        digests = 0
        # Daily digest: drives with registrations in the last 24 h, sent once per UTC day (first tick after 03:30 UTC
        # = 09:00 IST).
        if at.hour == 3:
            rows = (
                await db.execute(
                    select(CampusDrive.id, CampusDrive.owner_id, func.count(Registration.id))
                    .join(Registration, Registration.drive_id == CampusDrive.id)
                    .where(Registration.registered_at >= at - timedelta(days=1), CampusDrive.status.in_(("registration_open", "in_progress")))
                    .group_by(CampusDrive.id, CampusDrive.owner_id)
                )
            ).all()
            for drive_id, owner_id, count in rows:
                emit(db, CampusRegistrationsDigest(tenant_id=tid, drive_id=drive_id, owner_id=owner_id, count=int(count)))
                digests += 1
    return {"expired_invites": expired, "digests": digests}


@celery_app.task(name="campus.tick", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def tick(tenant_id: str) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))
    return run(lambda: tick_tenant(tid), tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="campus.tick_all", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def tick_all() -> int:
    ids = run(tenant_ids)
    for tid in ids:
        enqueue(tick, str(tid))
    return len(ids)
