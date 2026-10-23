# ruff: file-ignore[line-too-long]
"""Report builder Celery tasks (Epic 47; queue ``reports``, AD-54). Every tenant task starts with ``tenant_id``.

* ``reports.evaluate_schedules`` (beat, every 5 minutes) -> ``reports.evaluate_tenant(tenant_id)``: due schedules
  advance ``next_run_at`` and enqueue one ``reports.run_for_recipient(tenant_id, schedule_id, slot_at, user_id)``
  per recipient (after the evaluator's commit).
* ``reports.run_for_recipient`` -- one recipient's run of one slot, with that recipient's permissions (idempotent).
* ``reports.run_export(tenant_id, run_id)`` -- a builder export above the synchronous row limit.
* ``reports.validate_definitions`` (beat, nightly; ``maintenance``) -- the registry repair check of every definition.
* ``reports.purge_files`` (beat, hourly; ``maintenance``) -- delete expired report files (export retention).
"""

import uuid
from datetime import datetime
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "reports"
MAINTENANCE = "maintenance"


async def _evaluate(tenant_id: uuid.UUID) -> list[tuple[uuid.UUID, datetime, uuid.UUID]]:
    from app.services.reporting import builder

    async with tenant_session(tenant_id) as db:
        return await builder.evaluate_due(db)


@celery_app.task(name="reports.evaluate_tenant", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def evaluate_tenant(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    jobs = run(lambda: _evaluate(tid), tenant_id=tid)
    for schedule_id, slot_at, user_id in jobs:
        enqueue(run_for_recipient, str(tid), str(schedule_id), slot_at.isoformat(), str(user_id))
    return len(jobs)


@celery_app.task(name="reports.evaluate_schedules")  # type: ignore[misc,untyped-decorator,unused-ignore]
def evaluate_schedules() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(evaluate_tenant, str(tid))
    return len(ids)


async def _run_for_recipient(
    tenant_id: uuid.UUID, schedule_id: uuid.UUID, slot_at: datetime, user_id: uuid.UUID
) -> str:
    from app.services.reporting import builder

    async with tenant_session(tenant_id) as db:
        return await builder.run_for_recipient(db, schedule_id, slot_at, user_id)


@celery_app.task(name="reports.run_for_recipient", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def run_for_recipient(tenant_id: str, schedule_id: str, slot_at: str, user_id: str) -> Any:
    tid = uuid.UUID(str(tenant_id))
    return run(
        lambda: _run_for_recipient(
            tid, uuid.UUID(schedule_id), datetime.fromisoformat(slot_at), uuid.UUID(user_id)
        ),
        tenant_id=tid,
    )


async def _run_export(tenant_id: uuid.UUID, run_id: uuid.UUID) -> str:
    from app.services.reporting import builder

    async with tenant_session(tenant_id) as db:
        return await builder.run_export(db, run_id)


@celery_app.task(name="reports.run_export", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def run_export(tenant_id: str, run_id: str) -> Any:
    tid = uuid.UUID(str(tenant_id))
    return run(lambda: _run_export(tid, uuid.UUID(run_id)), tenant_id=tid)


async def _validate(tenant_id: uuid.UUID) -> dict[str, int]:
    from app.services.reporting import builder

    async with tenant_session(tenant_id) as db:
        return await builder.validate_all(db)


@celery_app.task(name="reports.validate_tenant", acks_late=True, queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def validate_tenant(tenant_id: str) -> Any:
    tid = uuid.UUID(str(tenant_id))
    return run(lambda: _validate(tid), tenant_id=tid)


@celery_app.task(name="reports.validate_definitions", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def validate_definitions() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(validate_tenant, str(tid))
    return len(ids)


async def _purge(tenant_id: uuid.UUID) -> int:
    from app.services.reporting.builder import governance

    async with tenant_session(tenant_id) as db:
        return await governance.purge_expired_files(db)


@celery_app.task(name="reports.purge_tenant_files", acks_late=True, queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_tenant_files(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _purge(tid), tenant_id=tid))


@celery_app.task(name="reports.purge_files", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_files() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(purge_tenant_files, str(tid))
    return len(ids)
