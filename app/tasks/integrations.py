# ruff: file-ignore[line-too-long]
"""Connector sync runner tasks (Epic 66, AD-81), queue ``ecosystem``: ``integrations.run_sync_job`` (one attempt of a
``sync:`` connector job; retries re-queue with the job's back-off) and ``integrations.fire_schedules`` (beat, 60 s)."""

import uuid

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "ecosystem"


async def _run(tenant_id: uuid.UUID, job_id: uuid.UUID) -> tuple[str, int | None]:
    from app.core.context import get_context
    from app.services.connectors import jobs

    async with tenant_session(tenant_id) as db:
        return await jobs.execute(get_context(), db, job_id)


@celery_app.task(name="integrations.run_sync_job", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def run_sync_job(tenant_id: str, job_id: str) -> str:
    tid = uuid.UUID(str(tenant_id))
    status, retry_in = run(lambda: _run(tid, uuid.UUID(str(job_id))), tenant_id=tid)
    # An uncounted reschedule (kill switch / rate limit) is not re-run inline by eager Celery (no countdown).
    if retry_in is not None and not (status == "deferred" and celery_app.conf.task_always_eager):
        try:
            run_sync_job.apply_async((tenant_id, job_id), countdown=retry_in)
        except Exception:  # pragma: no cover - broker down: the job stays failed, retryable by hand
            from loguru import logger

            logger.exception("Could not re-queue sync job {}", job_id)
    return str(status)


async def _fire(tenant_id: uuid.UUID) -> int:
    from app.core.context import get_context
    from app.services import integrations_service

    async with tenant_session(tenant_id) as db:
        return await integrations_service.fire_due(get_context(), db)


@celery_app.task(name="integrations.fire_schedules")  # type: ignore[misc,untyped-decorator,unused-ignore]
def fire_schedules() -> int:
    async def job() -> int:
        results = await for_each_tenant(_fire)
        return sum(v for v in results.values() if isinstance(v, int))

    return int(run(job))
