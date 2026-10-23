# ruff: file-ignore[line-too-long]
"""SLA and escalation Celery tasks (Epic 32; AD-9 tenant-scoped and idempotent, AD-37 queue ``maintenance``).

* ``sla.evaluate_clocks(tenant_id)`` / beat ``sla.evaluate_clocks_all`` (every ``SLA_EVAL_INTERVAL_SECONDS``);
* ``sla.evaluate_escalations(tenant_id)`` / beat ``sla.evaluate_escalations_all`` (every minute);
* ``sla.open_approval_watch(tenant_id, request_id, position)`` -- after ``ApprovalStepActivated`` commits;
* ``sla.close_watches(tenant_id, subject_kind, subject_id, outcome)`` -- after a request completes or a task is
  completed.
"""

import uuid

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "maintenance"


async def _evaluate_clocks(tenant_id: uuid.UUID) -> int:
    from app.services.sla import evaluator

    async with tenant_session(tenant_id) as db:
        return await evaluator.evaluate_clocks(db)


@celery_app.task(name="sla.evaluate_clocks", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def evaluate_clocks(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _evaluate_clocks(tid), tenant_id=tid))


@celery_app.task(name="sla.evaluate_clocks_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def evaluate_clocks_all() -> int:
    ids: list[uuid.UUID] = run(lambda: tenant_ids(("active",)))
    for tid in ids:
        enqueue(evaluate_clocks, str(tid))
    return len(ids)


async def _evaluate_escalations(tenant_id: uuid.UUID) -> int:
    from app.services.sla import escalations

    async with tenant_session(tenant_id) as db:
        return await escalations.evaluate_escalations(db)


@celery_app.task(name="sla.evaluate_escalations", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def evaluate_escalations(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _evaluate_escalations(tid), tenant_id=tid))


@celery_app.task(name="sla.evaluate_escalations_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def evaluate_escalations_all() -> int:
    ids: list[uuid.UUID] = run(lambda: tenant_ids(("active",)))
    for tid in ids:
        enqueue(evaluate_escalations, str(tid))
    return len(ids)


async def _open_approval_watch(tenant_id: uuid.UUID, request_id: uuid.UUID, position: int) -> str | None:
    from app.services.sla import escalations

    async with tenant_session(tenant_id) as db:
        watch_id = await escalations.open_approval_watch(db, request_id, position)
        return str(watch_id) if watch_id else None


@celery_app.task(name="sla.open_approval_watch", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def open_approval_watch(tenant_id: str, request_id: str, position: int) -> str | None:
    tid = uuid.UUID(str(tenant_id))
    result: str | None = run(
        lambda: _open_approval_watch(tid, uuid.UUID(str(request_id)), int(position)), tenant_id=tid
    )
    return result


async def _close(tenant_id: uuid.UUID, subject_kind: str, subject_id: uuid.UUID, outcome: str) -> int:
    from app.services.sla import escalations

    async with tenant_session(tenant_id) as db:
        return await escalations.close_watches(db, subject_kind, subject_id, outcome=outcome)


@celery_app.task(name="sla.close_watches", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def close_watches(tenant_id: str, subject_kind: str, subject_id: str, outcome: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _close(tid, subject_kind, uuid.UUID(str(subject_id)), outcome), tenant_id=tid))
