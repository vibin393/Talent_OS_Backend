"""Interview Celery tasks (Epic 7, AD-9). Every task takes ``tenant_id`` first, runs in ``tenant_session`` on a
private NullPool engine (``tasks.runtime.run``) and is idempotent.

* ``interviews.sync_interview_invites(tenant_id, interview_id, action)`` -- provider event or .ics emails.
* Beat (``app/job/schedules.py``), each fanning out one run per active tenant:
  - ``interviews.send_due_reminders_all`` every 5 minutes,
  - ``interviews.complete_elapsed_all`` every 15 minutes,
  - ``interviews.scorecard_reminders_all`` hourly.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "interactive"  # Epic 13 (AD-37): the queue of every task in this module


async def _in_tenant(tenant_id: uuid.UUID, work: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    from app.services import (  # ruff: ignore[unused-import] - wiring
        connections_service,
        interviews_service,
        notifications_service,
    )

    async with tenant_session(tenant_id) as db:
        audit.set_actor(db, "system", None)
        return await work(db)


def _run(tenant_id: str, work: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    tid = uuid.UUID(str(tenant_id))
    return run(lambda: _in_tenant(tid, work), tenant_id=tid)


@celery_app.task(name="interviews.sync_interview_invites", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def sync_interview_invites(tenant_id: str, interview_id: str, action: str = "request") -> int:
    from app.services import interviews_service

    async def work(db: AsyncSession) -> int:
        result = await interviews_service.sync_invites(db, uuid.UUID(str(interview_id)), action)
        return int(result.get("sent", 0))

    return int(_run(tenant_id, work))


@celery_app.task(name="interviews.send_due_reminders", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def send_due_reminders(tenant_id: str) -> int:
    from app.services import interviews_service

    return int(_run(tenant_id, interviews_service.send_due_reminders))


@celery_app.task(name="interviews.complete_elapsed", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def complete_elapsed(tenant_id: str) -> int:
    from app.services import interviews_service

    return int(_run(tenant_id, interviews_service.complete_elapsed))


@celery_app.task(name="interviews.scorecard_reminders", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def scorecard_reminders(tenant_id: str) -> int:
    from app.services import interviews_service

    return int(_run(tenant_id, interviews_service.scorecard_reminders))


def _fan_out(task: Any) -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(task, str(tid))
    return len(ids)


@celery_app.task(name="interviews.send_due_reminders_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def send_due_reminders_all() -> int:
    return _fan_out(send_due_reminders)


@celery_app.task(name="interviews.complete_elapsed_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def complete_elapsed_all() -> int:
    return _fan_out(complete_elapsed)


@celery_app.task(name="interviews.scorecard_reminders_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def scorecard_reminders_all() -> int:
    return _fan_out(scorecard_reminders)
