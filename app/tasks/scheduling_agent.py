# ruff: file-ignore[line-too-long]
"""Scheduling Agent tasks (Epic 59). Every task starts with ``tenant_id`` (beat fan-outs excepted). Queue ``maintenance``.

* ``scheduling_agent.run_needed`` -- a pick / counter-proposal: resume the waiting run or start a continuation run (C-12);
  retried shortly while a run is mid-step.
* ``scheduling_agent.interview_changed`` -- reminder rows on schedule / reschedule, superseded on cancel, scorecard nudges
  on completion; a cancelled interview closes its reschedule requests.
* ``scheduling_agent.scorecard_submitted`` -- stops that participant's nudges.
* ``scheduling_agent.stage_exit`` -- the Application left Submitted / Interviewing: open requests close, links revoked.
* ``scheduling_agent.run_failed`` -- a failed run hands its request to the recruiter.
* ``scheduling_agent.dispatch_reminders`` (beat, every 5 minutes) -- due reminders / nudges -> reminder runs; expired
  links; old reminder rows pruned (90 days after ``due_at``).
"""

import uuid
from datetime import timedelta
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "maintenance"
RETRY_SECONDS = 15
MAX_RETRIES = 8
REMINDER_RETENTION_DAYS = 90


@celery_app.task(name="scheduling_agent.run_needed", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def run_needed(
    tenant_id: str, request_id: str, trigger_key: str, offer_id: str | None, attempt: int = 0
) -> str:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> str:
        from app.services.scheduling_agent import requests

        async with tenant_session(tid) as db:
            return await requests.wake(
                db, uuid.UUID(str(request_id)), trigger_key, uuid.UUID(str(offer_id)) if offer_id else None
            )

    outcome = str(run(job, tenant_id=tid))
    if outcome == "retry" and attempt < MAX_RETRIES and not celery_app.conf.task_always_eager:
        run_needed.apply_async(
            args=(tenant_id, request_id, trigger_key, offer_id, attempt + 1),
            queue=QUEUE,
            countdown=RETRY_SECONDS,
        )
    return outcome


@celery_app.task(name="scheduling_agent.interview_changed", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def interview_changed(tenant_id: str, interview_id: str, change: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.scheduling_agent import reminders, requests

        iid = uuid.UUID(str(interview_id))
        async with tenant_session(tid) as db:
            if change == "scheduled":
                return await reminders.on_scheduled(db, iid)
            if change == "cancelled":
                await reminders.on_cancelled(db, iid)
                return await requests.close_for_interview(db, iid, "interview_cancelled")
            if change.startswith("completed:"):
                await requests.close_for_interview(db, iid, "interview_completed")
                return await reminders.on_completed(db, iid, change.split(":", 1)[1])
        return 0

    return int(run(job, tenant_id=tid))


@celery_app.task(name="scheduling_agent.scorecard_submitted", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def scorecard_submitted(tenant_id: str, interview_id: str, participant_id: str) -> None:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> None:
        from app.services.scheduling_agent import reminders

        async with tenant_session(tid) as db:
            await reminders.on_scorecard_submitted(
                db, uuid.UUID(str(interview_id)), uuid.UUID(str(participant_id))
            )

    run(job, tenant_id=tid)


@celery_app.task(name="scheduling_agent.stage_exit", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def stage_exit(tenant_id: str, application_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.scheduling_agent import requests

        async with tenant_session(tid) as db:
            return await requests.close_for_application(db, uuid.UUID(str(application_id)), "stage_exit")

    return int(run(job, tenant_id=tid))


@celery_app.task(name="scheduling_agent.run_failed", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def run_failed(tenant_id: str, run_id: str, reason_code: str) -> str | None:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> str | None:
        from app.services.scheduling_agent import requests

        async with tenant_session(tid) as db:
            return await requests.on_run_failed(db, uuid.UUID(str(run_id)), reason_code)

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


async def tick(db: Any) -> dict[str, int]:
    """One tenant's beat work (also called by tests)."""
    from sqlalchemy import delete

    from app.models.scheduling_agent import SchedulingReminder
    from app.services.scheduling_agent import reminders, requests
    from app.services.scheduling_agent.common import now

    out = await reminders.dispatch(db)
    out["expired"] = await requests.expire_due(db)
    pruned = await db.execute(
        delete(SchedulingReminder).where(
            SchedulingReminder.due_at < now() - timedelta(days=REMINDER_RETENTION_DAYS)
        )
    )
    out["pruned"] = int(getattr(pruned, "rowcount", 0) or 0)
    return out


@celery_app.task(name="scheduling_agent.dispatch_reminders", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def dispatch_reminders() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> Any:
        async with tenant_session(tid) as db:
            return await tick(db)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


__all__ = [
    "dispatch_reminders",
    "interview_changed",
    "run_failed",
    "run_needed",
    "scorecard_submitted",
    "stage_exit",
    "tick",
]
