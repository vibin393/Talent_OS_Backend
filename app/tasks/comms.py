"""Communication Celery tasks (AD-9, Epic 10).

* ``comms.send_email(tenant_id, message_id)`` -- idempotent delivery of one queued message (see
  ``comms_service.deliver``); a transient failure re-enqueues itself with backoff (30 s, 2 min, 10 min), the
  fourth failure marks the message ``failed``.
* ``comms.ensure_templates(tenant_id)`` -- seeds the system email templates of a new tenant.
"""

import uuid

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run

QUEUE = "interactive"  # Epic 13 (AD-37): the queue of every task in this module


async def _deliver(tenant_id: uuid.UUID, message_id: uuid.UUID) -> str:
    from app.services import comms_service

    async with tenant_session(tenant_id) as db:
        return await comms_service.deliver(db, message_id)


@celery_app.task(name="comms.send_email", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def send_email(tenant_id: str, message_id: str) -> str:
    from app.services import comms_service

    tid, mid = uuid.UUID(str(tenant_id)), uuid.UUID(str(message_id))
    outcome = str(run(lambda: _deliver(tid, mid), tenant_id=tid))
    if outcome == "retry":
        attempt = _attempts(tid, mid)
        backoff = comms_service.RETRY_BACKOFF_SECONDS
        countdown = backoff[min(max(attempt - 1, 0), len(backoff) - 1)]
        try:
            send_email.apply_async((str(tid), str(mid)), countdown=countdown)
        except Exception:  # broker down: the message stays queued (visible in the log)
            enqueue(send_email, str(tid), str(mid))
    return outcome


def _attempts(tenant_id: uuid.UUID, message_id: uuid.UUID) -> int:
    async def _read() -> int:
        from app.models.comms import Email

        async with tenant_session(tenant_id) as db:
            message = await db.get(Email, message_id)
            return message.attempts if message is not None else 0

    return int(run(_read, tenant_id=tenant_id))


async def _seed(tenant_id: uuid.UUID) -> int:
    from app.services import comms_service

    async with tenant_session(tenant_id) as db:
        return await comms_service.ensure_default_templates(db, tenant_id)


@celery_app.task(name="comms.ensure_templates", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def ensure_templates(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _seed(tid), tenant_id=tid))
