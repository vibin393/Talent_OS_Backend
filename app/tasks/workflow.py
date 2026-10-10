"""Workflow Celery tasks (Epic 10, story 10.4; AD-9).

* ``workflow.evaluate_event(tenant_id, trigger, entity_type, entity_id, event_id, extras, chain_depth)`` --
  enqueued by the trigger handlers with no countdown; idempotent per (rule, event, entity).
* ``workflow.purge_executions_all`` (beat, daily) -> ``workflow.purge_executions(tenant_id)`` (> 180 days).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "interactive"  # Epic 13 (AD-37): the queue of every task in this module


async def _evaluate(
    tenant_id: uuid.UUID,
    trigger: str,
    entity_type: str,
    entity_id: uuid.UUID,
    event_id: uuid.UUID,
    extras: dict[str, Any],
    chain_depth: int,
    enqueued_at: datetime,
) -> list[str]:
    from app.services import workflow

    async with tenant_session(tenant_id) as db:
        ids = await workflow.evaluate_event(
            db, trigger, entity_type, entity_id, event_id, extras, chain_depth, enqueued_at=enqueued_at
        )
        return [str(i) for i in ids]


@celery_app.task(name="workflow.evaluate_event", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def evaluate_event(
    tenant_id: str,
    trigger: str,
    entity_type: str,
    entity_id: str,
    event_id: str,
    extras: dict[str, Any] | None = None,
    chain_depth: int = 0,
) -> list[str]:
    tid = uuid.UUID(str(tenant_id))
    enqueued_at = datetime.now(UTC)
    return list(
        run(
            lambda: _evaluate(
                tid,
                trigger,
                entity_type,
                uuid.UUID(str(entity_id)),
                uuid.UUID(str(event_id)),
                dict(extras or {}),
                int(chain_depth),
                enqueued_at,
            ),
            tenant_id=tid,
        )
    )


async def _purge(tenant_id: uuid.UUID) -> int:
    from app.services import workflow

    async with tenant_session(tenant_id) as db:
        return await workflow.purge_executions(db)


@celery_app.task(name="workflow.purge_executions", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_executions(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _purge(tid), tenant_id=tid))


@celery_app.task(name="workflow.purge_executions_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_executions_all() -> int:
    ids: list[uuid.UUID] = run(lambda: tenant_ids(("active", "suspended")))
    for tid in ids:
        enqueue(purge_executions, str(tid))
    return len(ids)


# ================================================================ Epic 32: graph-run waits
async def _continue(tenant_id: uuid.UUID, wait_id: uuid.UUID) -> str | None:
    from app.services.workflow import waits

    async with tenant_session(tenant_id) as db:
        return await waits.continue_run(db, wait_id)


@celery_app.task(name="workflow.continue_run", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def continue_run(tenant_id: str, wait_id: str) -> str | None:
    """Continue a graph run after its approval was decided (enqueued after the decision commits)."""
    tid = uuid.UUID(str(tenant_id))
    result: str | None = run(lambda: _continue(tid, uuid.UUID(str(wait_id))), tenant_id=tid)
    return result


async def _resume(tenant_id: uuid.UUID) -> int:
    from app.services.workflow import waits

    async with tenant_session(tenant_id) as db:
        return await waits.resume_waits(db)


@celery_app.task(name="workflow.resume_waits", acks_late=True, queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def resume_waits(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _resume(tid), tenant_id=tid))


@celery_app.task(name="workflow.resume_waits_all", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def resume_waits_all() -> int:
    ids: list[uuid.UUID] = run(lambda: tenant_ids(("active",)))
    for tid in ids:
        enqueue(resume_waits, str(tid))
    return len(ids)
