# ruff: noqa: E501
"""Internal mobility Celery tasks (Epic 41). Queue ``maintenance`` (AD-54). Every task starts with ``tenant_id``
(except the beat fan-out).

* ``mobility.sync_opening(tenant_id, demand_id)``: after-commit handler of the P1 Demand events (subscribed in
  ``services/mobility/hooks.py``) -- upserts / closes the Demand's ``internal_openings`` row.
* ``mobility.reconcile_openings_all`` (beat, nightly) -> ``mobility.reconcile_openings(tenant_id)``: repairs drift.
"""

import uuid

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "maintenance"


async def sync_tenant_demand(tid: uuid.UUID, demand_id: uuid.UUID) -> str:
    from app.services.mobility import board

    async with tenant_session(tid) as db:
        return await board.sync_demand(db, demand_id)


@celery_app.task(name="mobility.sync_opening", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def sync_opening(tenant_id: str, demand_id: str) -> str:
    tid = uuid.UUID(str(tenant_id))
    return str(run(lambda: sync_tenant_demand(tid, uuid.UUID(str(demand_id))), tenant_id=tid))


async def reconcile_tenant(tid: uuid.UUID) -> int:
    from app.services.mobility import board

    async with tenant_session(tid) as db:
        return await board.reconcile(db)


@celery_app.task(name="mobility.reconcile_openings", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def reconcile_openings(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: reconcile_tenant(tid), tenant_id=tid) or 0)


@celery_app.task(name="mobility.reconcile_openings_all", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def reconcile_openings_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(reconcile_openings, str(tid))
    return len(ids)
