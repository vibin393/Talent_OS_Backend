# ruff: noqa: E501
"""Projects Celery tasks (Epic 45; queue ``maintenance``, AD-54). Every task starts with ``tenant_id`` (except
the fan-out).

* ``projects.rebuild_burn_all`` (beat, daily 02:30 IST) -> ``projects.rebuild_burn(tenant_id)``: rebuild the burn
  read model of every open project (posted facts only) and raise the 80 / 100 % budget alerts.
* ``projects.rebuild_burn_for(tenant_id, kind, ref_id)``: a fact source changed (``invoice`` issued / credited,
  ``placement`` timesheet approved) -> rebuild the affected projects.
* ``projects.placement_closed(tenant_id, placement_id, end_date, reason)``: ``PlacementEnded`` / ``PlacementDropped``
  -> end the Placement's active assignments (``ProjectAssignmentEnded`` once each).
"""

import uuid
from datetime import date
from typing import Any

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "maintenance"


async def _rebuild(tenant_id: uuid.UUID) -> int:
    from app.services.projects import burn

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        return await burn.rebuild_all(ctx, db)


@celery_app.task(name="projects.rebuild_burn", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def rebuild_burn(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _rebuild(tid), tenant_id=tid))


@celery_app.task(name="projects.rebuild_burn_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def rebuild_burn_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(rebuild_burn, str(tid))
    return len(ids)


async def _rebuild_for(tenant_id: uuid.UUID, kind: str, ref_id: uuid.UUID) -> int:
    from app.services.projects import burn

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        ids = await burn.projects_for(db, kind, ref_id)
        return await burn.rebuild_many(ctx, db, ids)


@celery_app.task(name="projects.rebuild_burn_for", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def rebuild_burn_for(tenant_id: str, kind: str, ref_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _rebuild_for(tid, kind, uuid.UUID(ref_id)), tenant_id=tid))


async def _placement_closed(
    tenant_id: uuid.UUID, placement_id: uuid.UUID, end_date: date, reason: str | None
) -> int:
    from app.services.projects import assignments

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        return await assignments.end_for_placement(ctx, db, placement_id, end_date, reason)


@celery_app.task(name="projects.placement_closed", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def placement_closed(tenant_id: str, placement_id: str, end_date: str, reason: str | None = None) -> Any:
    tid = uuid.UUID(str(tenant_id))
    return run(
        lambda: _placement_closed(tid, uuid.UUID(placement_id), date.fromisoformat(end_date), reason),
        tenant_id=tid,
    )
