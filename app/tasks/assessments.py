# ruff: file-ignore[line-too-long]
"""Assessment Celery tasks (Epic 40). Every task starts with ``tenant_id`` (except the beat fan-outs).

* ``assessments.reminders_all`` (beat, hourly) -> ``assessments.reminders(tenant_id)``: one reminder per open invitation
  within ``reminder_hours_before`` of its deadline (queue ``maintenance``).
* ``assessments.expire_all`` (beat, every 5 minutes) -> ``assessments.expire(tenant_id)``: ``invited`` past the
  deadline -> ``expired`` (system), link revoked (queue ``maintenance``).
* ``assessments.auto_submit_all`` (beat, every minute) -> ``assessments.auto_submit(tenant_id)``: ``in_progress``
  attempts past ``deadline_at + 30 s`` -> ``auto_submitted`` and scored (queue ``interactive``).
* ``assessments.provider_invite(tenant_id, job_id)`` (queue ``connectors``): run the provider invite job, store the
  provider reference, email the take URL; bounded retries via the connector job back-off.
"""

import uuid

from app.core.celery import celery_app
from app.core.context import get_context
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "maintenance"


async def _reminders(tid: uuid.UUID) -> int:
    from app.services.assessments import invitations

    async with tenant_session(tid) as db:
        return await invitations.send_reminders(db)


async def _expire(tid: uuid.UUID) -> int:
    from app.services.assessments import invitations

    async with tenant_session(tid) as db:
        return await invitations.expire_due(db)


async def _auto_submit(tid: uuid.UUID) -> int:
    from app.services.assessments import taking

    async with tenant_session(tid) as db:
        return await taking.auto_submit_due(db)


async def _provider_invite(tid: uuid.UUID, job_id: uuid.UUID) -> int | None:
    from app.services.assessments import provider

    async with tenant_session(tid) as db:
        return await provider.run_invite(get_context(), db, job_id)


@celery_app.task(name="assessments.reminders", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def reminders(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _reminders(tid), tenant_id=tid) or 0)


@celery_app.task(name="assessments.expire", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def expire(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _expire(tid), tenant_id=tid) or 0)


@celery_app.task(name="assessments.auto_submit", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def auto_submit(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _auto_submit(tid), tenant_id=tid) or 0)


@celery_app.task(name="assessments.provider_invite", queue="connectors", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def provider_invite(tenant_id: str, job_id: str) -> str:
    tid = uuid.UUID(str(tenant_id))
    retry_in = run(lambda: _provider_invite(tid, uuid.UUID(str(job_id))), tenant_id=tid)
    if retry_in is not None:
        try:
            provider_invite.apply_async((tenant_id, job_id), countdown=retry_in)
        except Exception:  # pragma: no cover - broker down: the job stays failed, retryable by hand
            from loguru import logger

            logger.exception("Could not re-queue assessment provider invite {}", job_id)
        return "retry"
    return "done"


def _fan_out(task: object) -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(task, str(tid))
    return len(ids)


@celery_app.task(name="assessments.reminders_all", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def reminders_all() -> int:
    return _fan_out(reminders)


@celery_app.task(name="assessments.expire_all", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def expire_all() -> int:
    return _fan_out(expire)


@celery_app.task(name="assessments.auto_submit_all", queue="interactive")  # type: ignore[misc,untyped-decorator,unused-ignore]
def auto_submit_all() -> int:
    return _fan_out(auto_submit)
