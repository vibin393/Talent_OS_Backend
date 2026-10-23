# ruff: file-ignore[line-too-long]
"""Outbox relay tasks (Epic 66, AD-79), queue ``webhooks``: ``public_events.dispatch`` / ``sweep``; ``public_events.purge`` on ``maintenance``.

* ``public_events.dispatch(tenant_id)`` -- enqueued after commit (deduplicated per tenant for 2 s); re-enqueues itself
  while full batches remain.
* ``public_events.sweep`` (beat, every 30 s) -- crash recovery: dispatches every active tenant with pending rows older
  than 30 s (the outbox is RLS'd, so tenants are iterated from the platform ``tenants`` table).
* ``public_events.purge`` (beat, daily) -- deletes dispatched rows older than ``public_event_retention_days``.
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.tasks.runtime import for_each_tenant, run

QUEUE = "webhooks"
BATCH = 500


@celery_app.task(name="public_events.dispatch", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def dispatch(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services import public_events_service

        total = 0
        while True:
            n = await public_events_service.dispatch(tid, batch=BATCH)
            total += n
            if n < BATCH:
                return total

    return int(run(job, tenant_id=tid))


@celery_app.task(name="public_events.sweep", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def sweep() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> int:
        from app.services import public_events_service

        return await public_events_service.sweep_tenant(tid, batch=BATCH)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


@celery_app.task(name="public_events.purge", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> int:
        from app.core.settings import get_settings
        from app.services import public_events_service

        return await public_events_service.purge(
            tid, older_than_days=get_settings().public_event_retention_days
        )

    return run(lambda: for_each_tenant(job, ("active", "suspended", "offboarding")))  # type: ignore[no-any-return]


__all__ = ["dispatch", "purge", "sweep"]
