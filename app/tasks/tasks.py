"""Task reminders (Epic 10, story 10.2; AD-9).

* ``tasks.send_reminders(tenant_id)`` claims due reminders of one tenant (``FOR UPDATE SKIP LOCKED``), stamps
  ``reminded_at`` and emits ``TaskReminderDue`` (notifications fan out after commit). Idempotent.
* ``tasks.send_reminders_all`` (beat, every minute) enqueues one run per active tenant.
"""

import uuid

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "interactive"  # Epic 13 (AD-37): the queue of every task in this module


async def _send(tenant_id: uuid.UUID) -> int:
    from app.services import notifications_service, tasks_service

    notifications_service.register()  # the TaskReminderDue handler must exist in this process
    async with tenant_session(tenant_id) as db:
        return await tasks_service.send_reminders(db)


@celery_app.task(name="tasks.send_reminders", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def send_reminders(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _send(tid), tenant_id=tid))


@celery_app.task(name="tasks.send_reminders_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def send_reminders_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(send_reminders, str(tid))
    return len(ids)
