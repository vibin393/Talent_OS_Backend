# ruff: file-ignore[line-too-long]
"""Knowledge tasks (Epic 62). Queue ``maintenance``.

* ``knowledge.reindex`` -- embed chunks without an embedding (local model; never a Claude call).
* ``knowledge.reconcile`` (beat ``knowledge-reconcile``, every 5 minutes) -- re-asserts every chunk's ``active`` flag.
* ``knowledge.run_failed`` -- a failed onboarding run hands the question to the coordinator.
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "maintenance"


@celery_app.task(name="knowledge.reindex", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def reindex(tenant_id: str, source_id: str | None = None) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.knowledge import index

        async with tenant_session(tid) as db:
            return await index.reindex(db, uuid.UUID(source_id) if source_id else None)

    return int(run(job, tenant_id=tid))


@celery_app.task(name="knowledge.reconcile", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def reconcile() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> int:
        from app.services.knowledge import index

        async with tenant_session(tid) as db:
            return await index.reconcile(db)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


@celery_app.task(name="knowledge.run_failed", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def run_failed(tenant_id: str, run_id: str, reason: str | None) -> bool:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> bool:
        from app.services.knowledge import qa

        async with tenant_session(tid) as db:
            return await qa.run_failed(db, uuid.UUID(run_id), reason or "failed")

    return bool(run(job, tenant_id=tid))


__all__ = ["reconcile", "reindex", "run_failed"]
