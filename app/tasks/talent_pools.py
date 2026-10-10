# ruff: noqa: E501
"""Talent pool Celery tasks (Epic 38). Queue ``maintenance`` (AD-54). Every task starts with ``tenant_id``
(except the beat fan-out).

* ``talent_pools.refresh(tenant_id, refresh_id)``: run a requested refresh (``PoolRefreshRequested``, after commit).
* ``talent_pools.graph_changed(tenant_id, scope, skill_keys)``: ``SkillGraphChanged`` -> mark affected dynamic
  Pools due (``refresh_due_at``).
* ``talent_pools.tick_all`` (beat, every 15 minutes) -> ``talent_pools.tick(tenant_id)``: tenant-local nightly
  refresh + segment repair check, the daily digest, and due Pools (refreshed within the hour of a graph change).
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "maintenance"


async def _refresh(tid: uuid.UUID, refresh_id: uuid.UUID) -> dict[str, Any]:
    from app.services.talent_pools import refresh

    async with tenant_session(tid) as db:
        return await refresh.run_refresh(db, tid, refresh_id)


@celery_app.task(name="talent_pools.refresh", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def refresh(tenant_id: str, refresh_id: str) -> dict[str, Any]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _refresh(tid, uuid.UUID(str(refresh_id))), tenant_id=tid) or {})


async def _graph_changed(tid: uuid.UUID, scope: str, keys: list[str]) -> int:
    from app.services.talent_pools import refresh as pool_refresh

    async with tenant_session(tid) as db:
        return await pool_refresh.mark_due_for_graph_change(db, tid, scope, keys)


@celery_app.task(name="talent_pools.graph_changed", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def graph_changed(tenant_id: str, scope: str, skill_keys: list[str] | None = None) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _graph_changed(tid, str(scope), list(skill_keys or [])), tenant_id=tid) or 0)


async def _tick(tid: uuid.UUID) -> dict[str, int]:
    from app.services.talent_pools import digest

    async with tenant_session(tid) as db:
        return await digest.tick(db, tid)


@celery_app.task(name="talent_pools.tick", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def tick(tenant_id: str) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _tick(tid), tenant_id=tid) or {})


@celery_app.task(name="talent_pools.tick_all", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def tick_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(tick, str(tid))
    return len(ids)
