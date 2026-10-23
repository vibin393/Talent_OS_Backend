# ruff: file-ignore[line-too-long]
"""Agent runtime Celery tasks (Epic 53, AD-58 / AD-68). Every task starts with ``tenant_id`` (beat fan-outs excepted).

* ``agents.run_step(tenant_id, run_id, step_no, attempt=0)`` -- one step; dispatched with ``apply_async(queue=...)`` on
  ``agents`` (background) or ``conversations`` (Copilot / conversation runs); enqueues the next step itself.
* ``agents.start_from_event`` / ``agents.resume_event`` -- event triggers and waits (queue ``agents``).
* Beat (queue ``maintenance``): ``reap_stalled`` 60 s, ``expire_proposals`` 900 s, ``budget_alerts`` hourly,
  ``purge_payloads`` daily, ``fire_schedules`` every minute; ``stop_all_tenants`` after the global kill switch.
"""

import socket
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, for_each_tenant, run

QUEUE = "agents"
MAINTENANCE = "maintenance"


def _worker_id() -> str:
    return f"{socket.gethostname()[:40]}:{uuid.uuid4().hex[:12]}"


@celery_app.task(name="agents.run_step", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def run_step(tenant_id: str, run_id: str, step_no: int, attempt: int = 0) -> str | None:
    from app.services.agent_runtime import executor

    tid, rid = uuid.UUID(str(tenant_id)), uuid.UUID(str(run_id))
    outcome = run(
        lambda: executor.run_step(tid, rid, int(step_no), worker_id=_worker_id(), attempt=int(attempt)),
        tenant_id=tid,
    )
    if outcome.next_step is not None:
        next_attempt = attempt + 1 if outcome.note == "provider_retry" else 0
        queue = run(lambda: _queue_of(tid, rid), tenant_id=tid)
        if celery_app.conf.task_always_eager:
            run_step.apply(args=(str(tid), str(rid), outcome.next_step, next_attempt))
        else:
            run_step.apply_async(
                args=(str(tid), str(rid), outcome.next_step, next_attempt),
                queue=queue,
                countdown=outcome.countdown or None,
            )
    return outcome.status or outcome.note or None


async def _queue_of(tenant_id: uuid.UUID, run_id: uuid.UUID) -> str:
    from app.models.agent_runtime import AgentRun
    from app.services.agent_runtime.executor import queue_for_origin

    async with tenant_session(tenant_id) as db:
        row = await db.get(AgentRun, run_id)
        return queue_for_origin(row.origin if row is not None else "agent")


@celery_app.task(name="agents.start_from_event", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def start_from_event(
    tenant_id: str,
    agent_key: str,
    trigger_key: str,
    subject_type: str | None,
    subject_id: str | None,
    dedup_key: str | None,
) -> str | None:
    tid = uuid.UUID(str(tenant_id))

    async def _job() -> str | None:
        from app.services.agent_runtime import runs as svc

        subject = (subject_type, uuid.UUID(subject_id)) if subject_type and subject_id else None
        async with tenant_session(tid) as db:
            ref = await svc.start_run(
                RequestContext(tenant_id=tid, actor_kind="system"), db, agent_key=agent_key, trigger_kind="event",
                trigger_key=trigger_key, subject=subject, dedup_key=dedup_key,
            )  # fmt: skip
        return str(ref.run_id) if ref.run_id else ref.reason

    return run(_job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="agents.resume_event", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def resume_event(tenant_id: str, condition: str, ref: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def _job() -> int:
        from app.services.agent_runtime import runs as svc

        async with tenant_session(tid) as db:
            refs = await svc.resume(
                RequestContext(tenant_id=tid, actor_kind="system"),
                db,
                condition=condition,
                ref=uuid.UUID(ref),
            )
        return len(refs)

    return int(run(_job, tenant_id=tid))


# ---------------------------------------------------------------- beat fan-outs (queue maintenance)
def _per_tenant(fn_name: str) -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> Any:
        from app.services.agent_runtime import maintenance

        async with tenant_session(tid) as db:
            if fn_name == "budget_alerts":
                return await maintenance.budget_alerts(db, tid)
            return await getattr(maintenance, fn_name)(db)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


@celery_app.task(name="agents.reap_stalled", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def reap_stalled() -> dict[str, Any]:
    return _per_tenant("reap")


@celery_app.task(name="agents.expire_proposals", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def expire_proposals() -> dict[str, Any]:
    return _per_tenant("expire_proposals")


@celery_app.task(name="agents.budget_alerts", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def budget_alerts() -> dict[str, Any]:
    return _per_tenant("budget_alerts")


@celery_app.task(name="agents.purge_payloads", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_payloads() -> dict[str, Any]:
    return _per_tenant("purge_payloads")


@celery_app.task(name="agents.close_idle_external", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def close_idle_external() -> dict[str, Any]:
    """Epic 66 (§0.15): stop MCP sessions idle > 30 minutes (every 5 minutes)."""
    return _per_tenant("close_idle_external")


@celery_app.task(name="agents.fire_schedules", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def fire_schedules() -> dict[str, Any]:
    at = datetime.now(UTC).replace(second=0, microsecond=0)

    async def job(tid: uuid.UUID) -> int:
        from app.services.agent_runtime.triggers import fire_schedules as fire

        async with tenant_session(tid) as db:
            return await fire(RequestContext(tenant_id=tid, actor_kind="system"), db, at)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


@celery_app.task(name="agents.stop_all_tenants", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def stop_all_tenants() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> Any:
        from app.services.agent_runtime.admin import stop_runs

        async with tenant_session(tid) as db:
            return await stop_runs(db)

    return run(lambda: for_each_tenant(job, ("active", "suspended")))  # type: ignore[no-any-return]


__all__ = [
    "budget_alerts",
    "close_idle_external",
    "enqueue",
    "expire_proposals",
    "fire_schedules",
    "purge_payloads",
    "reap_stalled",
    "resume_event",
    "run_step",
    "start_from_event",
    "stop_all_tenants",
]
