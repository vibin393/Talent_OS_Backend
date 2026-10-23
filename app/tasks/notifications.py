"""Notification Celery tasks (Epic 10, story 10.3; AD-9). Every task starts with ``tenant_id`` and is
idempotent.

* ``notifications.fan_out(tenant_id, type, payload)`` -- one domain event -> notifications (+ emails).
* ``notifications.digest_tick_all`` (beat, hourly at :05) -> ``notifications.digest_tick(tenant_id)``.
* ``notifications.purge_old_all`` (beat, daily) -> ``notifications.purge_old(tenant_id)``.
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "interactive"  # Epic 13 (AD-37): the queue of every task in this module


async def _fan_out(tenant_id: uuid.UUID, type_key: str, payload: dict[str, Any]) -> int:
    from app.services import notifications_service

    async with tenant_session(tenant_id) as db:
        return await notifications_service.fan_out(db, type_key, payload)


@celery_app.task(name="notifications.fan_out", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def fan_out(tenant_id: str, type_key: str, payload: dict[str, Any]) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _fan_out(tid, type_key, dict(payload)), tenant_id=tid))


async def _digest(tenant_id: uuid.UUID) -> int:
    from app.services import notifications_service

    async with tenant_session(tenant_id) as db:
        return await notifications_service.run_digest(db)


@celery_app.task(name="notifications.digest_tick", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def digest_tick(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _digest(tid), tenant_id=tid))


@celery_app.task(name="notifications.digest_tick_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def digest_tick_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(digest_tick, str(tid))
    return len(ids)


async def _purge(tenant_id: uuid.UUID) -> int:
    from app.services import notifications_service

    async with tenant_session(tenant_id) as db:
        return await notifications_service.purge_old(db)


@celery_app.task(name="notifications.purge_old", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_old(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _purge(tid), tenant_id=tid))


@celery_app.task(name="notifications.purge_old_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_old_all() -> int:
    ids: list[uuid.UUID] = run(lambda: tenant_ids(("active", "suspended")))
    for tid in ids:
        enqueue(purge_old, str(tid))
    return len(ids)


def _wire_handlers() -> None:
    """Worker processes: register the Epic 10 event handlers (the API registers them through its routers)."""
    from app.services import (  # ruff: ignore[unused-import]  # noqa: F401
        comms_service,
        notifications_service,
        workflow,
    )


_wire_handlers()
