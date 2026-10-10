"""Connector Celery tasks (Epic 13, AD-29), queue ``connectors``. Every task starts with ``tenant_id``.

* ``connectors.run_job(tenant_id, job_id)``: one attempt; failures re-queue with 30 s x 2^n back-off (at most
  ``max_attempts``), then the job is ``dead`` (``ConnectorJobFailed``).
* ``connectors.process_inbound(tenant_id, event_id)``: hand a verified webhook payload to the category handler.
"""

import uuid

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import run

QUEUE = "connectors"


async def _run_job(tenant_id: uuid.UUID, job_id: uuid.UUID) -> tuple[str, int | None]:
    from app.core.context import get_context
    from app.services.connectors import jobs

    async with tenant_session(tenant_id) as db:
        return await jobs.execute(get_context(), db, job_id)


@celery_app.task(name="connectors.run_job", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def run_job(tenant_id: str, job_id: str) -> str:
    tid = uuid.UUID(str(tenant_id))
    status, retry_in = run(lambda: _run_job(tid, uuid.UUID(str(job_id))), tenant_id=tid)
    # Epic 66: an uncounted reschedule (kill switch / rate limit) is not re-run inline by eager Celery (no countdown).
    if retry_in is not None and not (status == "deferred" and celery_app.conf.task_always_eager):
        try:
            run_job.apply_async((tenant_id, job_id), countdown=retry_in)
        except Exception:  # pragma: no cover - broker down: the job stays failed, retryable by hand
            from loguru import logger

            logger.exception("Could not re-queue connector job {}", job_id)
    return str(status)


async def _process_inbound(tenant_id: uuid.UUID, event_id: uuid.UUID) -> str:
    from app.core.context import get_context
    from app.services.connectors import jobs

    async with tenant_session(tenant_id) as db:
        return await jobs.process_inbound(get_context(), db, event_id)


@celery_app.task(name="connectors.process_inbound", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def process_inbound(tenant_id: str, event_id: str) -> str:
    tid = uuid.UUID(str(tenant_id))
    return str(run(lambda: _process_inbound(tid, uuid.UUID(str(event_id))), tenant_id=tid))


async def _release_held(tenant_id: uuid.UUID) -> int:
    from app.services.connectors import service

    return len(await service.release_held(tenant_id))


@celery_app.task(name="connectors.release_held", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def release_held(tenant_id: str) -> int:
    """Epic 66 (FR-340): process the tenant's ``held`` inbound webhooks in order once the kill switch is lifted."""
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _release_held(tid), tenant_id=tid))
