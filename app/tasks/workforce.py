# ruff: file-ignore[line-too-long]
"""Workforce planning Celery tasks (Epic 44; queue ``maintenance``, AD-54). Every task starts with ``tenant_id``.

Slot status follows P1 / P2 events (``services/workforce/hooks.py`` enqueues after commit):

* ``workforce.demand_opened(tenant_id, demand_id)`` -> slot ``requisitioned`` (or a manual Demand claims a slot);
* ``workforce.placement_activated(tenant_id, placement_id)`` -> slot ``filled``;
* ``workforce.demand_status_changed(tenant_id, demand_id, to_status)`` -> cancelled / closed without a Placement
  releases the slot.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import run

QUEUE = "maintenance"


async def _in_tenant(tenant_id: uuid.UUID, fn: Callable[[Any], Awaitable[uuid.UUID | None]]) -> str | None:
    async with tenant_session(tenant_id) as db:
        result = await fn(db)
    return str(result) if result else None


@celery_app.task(name="workforce.demand_opened", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def demand_opened(tenant_id: str, demand_id: str) -> str | None:
    from app.services.workforce import slots

    tid, did = uuid.UUID(str(tenant_id)), uuid.UUID(str(demand_id))
    return run(lambda: _in_tenant(tid, lambda db: slots.on_demand_opened(db, did)), tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="workforce.placement_activated", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def placement_activated(tenant_id: str, placement_id: str) -> str | None:
    from app.services.workforce import slots

    tid, pid = uuid.UUID(str(tenant_id)), uuid.UUID(str(placement_id))
    return run(lambda: _in_tenant(tid, lambda db: slots.on_placement_activated(db, pid)), tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="workforce.demand_status_changed", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def demand_status_changed(tenant_id: str, demand_id: str, to_status: str) -> str | None:
    from app.services.workforce import slots

    tid, did = uuid.UUID(str(tenant_id)), uuid.UUID(str(demand_id))
    result: str | None = run(
        lambda: _in_tenant(tid, lambda db: slots.on_demand_status_changed(db, did, to_status)), tenant_id=tid
    )
    return result
