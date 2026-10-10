"""Compliance Celery tasks (Epic 21). Every task starts with ``tenant_id`` (except the beat fan-out).

* ``compliance.evaluate(tenant_id, subject_kind, subject_id)`` (queue ``interactive``): ``PlacementCreated`` /
  ``ApplicationStageChanged(selected)`` -> checklist + ``ComplianceRequirementsEvaluated``. Idempotent.
* ``compliance.close(tenant_id, placement_id)``: ``PlacementDropped`` closes the checklist (no more alerts).
* ``compliance.policy_ack(tenant_id, candidate_id, template_id)``: ``PolicyAcknowledged``.
* ``compliance.reevaluate_active(tenant_id, run_id)``: a requirement was created / changed.
* ``compliance.recompute_all`` (beat, hourly) -> ``compliance.recompute_expiry(tenant_id)`` (queue
  ``maintenance``): runs when the tenant-local hour is ``recompute_hour_local``, once per local date.
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "maintenance"


def _ctx(tid: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tid, actor_kind="system")


async def _evaluate(tid: uuid.UUID, kind: str, subject_id: uuid.UUID) -> int:
    from app.services.compliance import checklist

    async with tenant_session(tid) as db:
        return await checklist.evaluate_subject(_ctx(tid), db, kind, subject_id)


@celery_app.task(name="compliance.evaluate", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def evaluate(tenant_id: str, subject_kind: str, subject_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _evaluate(tid, subject_kind, uuid.UUID(str(subject_id))), tenant_id=tid) or 0)


async def _close(tid: uuid.UUID, placement_id: uuid.UUID) -> int:
    from app.services.compliance import checklist

    async with tenant_session(tid) as db:
        return await checklist.close_placement(db, placement_id)


@celery_app.task(name="compliance.close", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def close(tenant_id: str, placement_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _close(tid, uuid.UUID(str(placement_id))), tenant_id=tid) or 0)


async def _policy_ack(tid: uuid.UUID, candidate_id: uuid.UUID, template_id: uuid.UUID) -> int:
    from app.services.compliance import checklist

    async with tenant_session(tid) as db:
        return await checklist.policy_acknowledged(db, candidate_id, template_id)


@celery_app.task(name="compliance.policy_ack", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def policy_ack(tenant_id: str, candidate_id: str, template_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(
        run(
            lambda: _policy_ack(tid, uuid.UUID(str(candidate_id)), uuid.UUID(str(template_id))), tenant_id=tid
        )
        or 0
    )


async def _reevaluate(tid: uuid.UUID, run_id: uuid.UUID) -> int:
    from app.services.compliance import checklist

    async with tenant_session(tid) as db:
        r = await checklist.reevaluate_active(_ctx(tid), db, run_id)
        return int(r.placements_evaluated)


@celery_app.task(name="compliance.reevaluate_active", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def reevaluate_active(tenant_id: str, run_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _reevaluate(tid, uuid.UUID(str(run_id))), tenant_id=tid) or 0)


async def _recompute(tid: uuid.UUID, force: bool) -> dict[str, Any]:
    from app.services.compliance import recompute

    async with tenant_session(tid) as db:
        return await recompute.run(_ctx(tid), db, force=force)


@celery_app.task(name="compliance.recompute_expiry", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def recompute_expiry(tenant_id: str, force: bool = False) -> dict[str, Any]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _recompute(tid, force), tenant_id=tid) or {})


@celery_app.task(name="compliance.recompute_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def recompute_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(recompute_expiry, str(tid))
    return len(ids)


# Epic 46 (AD-52): ``PermitRecorded`` -> P2 Credential (``compliance.hooks.record_permit_credential``).
async def _permit_recorded(
    tid: uuid.UUID,
    permit_id: uuid.UUID,
    candidate_id: uuid.UUID,
    type_key: str,
    country: str,
    valid_from: str,
    valid_to: str | None,
) -> str | None:
    from datetime import date

    from app.services.compliance.hooks import record_permit_credential

    async with tenant_session(tid) as db:
        cid = await record_permit_credential(
            db,
            permit_id=permit_id,
            candidate_id=candidate_id,
            credential_type_key=type_key,
            country=country,
            valid_from=date.fromisoformat(valid_from),
            valid_to=date.fromisoformat(valid_to) if valid_to else None,
        )
        return str(cid) if cid else None


@celery_app.task(name="compliance.permit_recorded", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def permit_recorded(
    tenant_id: str,
    permit_id: str,
    candidate_id: str,
    type_key: str,
    country: str,
    valid_from: str,
    valid_to: str | None = None,
) -> str | None:
    tid = uuid.UUID(str(tenant_id))
    result = run(
        lambda: _permit_recorded(
            tid,
            uuid.UUID(str(permit_id)),
            uuid.UUID(str(candidate_id)),
            type_key,
            country,
            valid_from,
            valid_to,
        ),
        tenant_id=tid,
    )
    return str(result) if result else None
