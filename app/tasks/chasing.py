# ruff: noqa: E501
"""Chasing tasks (Epic 62). Queue ``maintenance``.

* ``chasing.compliance_tick`` / ``chasing.bgv_tick`` (beats, hourly) -- deterministic run starters at the tenant's
  local run hour; no model call when nothing is due.
* ``chasing.placement_created`` -- a Compliance Agent run for a new Placement when an item is already due.
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "maintenance"


@celery_app.task(name="chasing.compliance_tick", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def compliance_tick() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> Any:
        from app.services.chasing import ticks

        async with tenant_session(tid) as db:
            return await ticks.compliance_tick(db)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


@celery_app.task(name="chasing.bgv_tick", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def bgv_tick() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> Any:
        from app.services.chasing import ticks

        async with tenant_session(tid) as db:
            return await ticks.bgv_tick(db)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


@celery_app.task(name="chasing.placement_created", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def placement_created(tenant_id: str, candidate_id: str) -> str | None:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> str | None:
        from app.services.chasing import ticks

        async with tenant_session(tid) as db:
            return await ticks.start_for_candidate(db, uuid.UUID(candidate_id), "placement_created")

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


__all__ = ["bgv_tick", "compliance_tick", "placement_created"]
