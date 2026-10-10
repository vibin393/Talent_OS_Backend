# ruff: noqa: E501
"""Referral Celery tasks (Epic 42). Queue ``maintenance`` (AD-54). Every task starts with ``tenant_id`` (except
the beat fan-outs).

* ``referrals.stage_changed`` / ``referrals.placement_event`` / ``referrals.payroll_finalized``: after-commit
  handlers of ``ApplicationStageChanged``, ``PlacementCreated|Activated|Ended|Dropped`` and
  ``PayrollExportFinalized`` (subscribed in ``services/referrals/hooks.py``).
* ``referrals.expire_all`` (beat) -> ``referrals.expire(tenant_id)``: referee links that ran out -> ``expired``.
* ``referrals.evaluate_rewards_all`` (beat) -> ``referrals.evaluate_rewards(tenant_id)``: due rewards -> eligible.
"""

import uuid
from datetime import date

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "maintenance"


async def _stage(tid: uuid.UUID, application_id: uuid.UUID, to_key: str) -> str | None:
    from app.services.referrals import status

    async with tenant_session(tid) as db:
        return await status.on_stage_changed(db, application_id, to_key)


@celery_app.task(name="referrals.stage_changed", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def stage_changed(tenant_id: str, application_id: str, to_key: str) -> str | None:
    tid = uuid.UUID(str(tenant_id))
    return run(lambda: _stage(tid, uuid.UUID(str(application_id)), to_key), tenant_id=tid)  # type: ignore[no-any-return]


async def _placement(tid: uuid.UUID, placement_id: uuid.UUID, kind: str, when: str | None) -> None:
    from app.services.referrals import status

    async with tenant_session(tid) as db:
        if kind == "created":
            await status.on_placement_created(db, placement_id)
        elif kind == "activated":
            await status.on_placement_activated(db, placement_id)
        elif kind == "ended" and when:
            await status.on_placement_ended(db, placement_id, date.fromisoformat(when))
        elif kind == "dropped":
            await status.on_placement_dropped(db, placement_id)


@celery_app.task(name="referrals.placement_event", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def placement_event(tenant_id: str, placement_id: str, kind: str, when: str | None) -> None:
    tid = uuid.UUID(str(tenant_id))
    run(lambda: _placement(tid, uuid.UUID(str(placement_id)), kind, when), tenant_id=tid)


async def _finalized(tid: uuid.UUID, version_id: uuid.UUID) -> int:
    from app.services.referrals import posting_source

    async with tenant_session(tid) as db:
        return await posting_source.mark_paid_for_export(db, version_id)


@celery_app.task(name="referrals.payroll_finalized", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def payroll_finalized(tenant_id: str, export_version_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _finalized(tid, uuid.UUID(str(export_version_id))), tenant_id=tid) or 0)


async def expire_tenant(tid: uuid.UUID) -> int:
    from app.core import audit
    from app.services.referrals import acceptance

    async with tenant_session(tid) as db:
        audit.set_actor(db, "system", None)
        return await acceptance.expire_due(db)


@celery_app.task(name="referrals.expire", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def expire(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: expire_tenant(tid), tenant_id=tid) or 0)


@celery_app.task(name="referrals.expire_all", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def expire_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(expire, str(tid))
    return len(ids)


async def evaluate_tenant(tid: uuid.UUID, on: date | None = None) -> int:
    from app.core import audit
    from app.services.referrals import rewards

    async with tenant_session(tid) as db:
        audit.set_actor(db, "system", None)
        return await rewards.evaluate(db, on)


@celery_app.task(name="referrals.evaluate_rewards", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def evaluate_rewards(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: evaluate_tenant(tid), tenant_id=tid) or 0)


@celery_app.task(name="referrals.evaluate_rewards_all", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def evaluate_rewards_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(evaluate_rewards, str(tid))
    return len(ids)
