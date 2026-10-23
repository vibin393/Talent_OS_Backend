# ruff: file-ignore[line-too-long]
"""AI campaign copy Celery tasks (Epic 78). Every per-tenant task starts with ``tenant_id``.

* ``campaign_copy.finalize_run(tenant_id, run_id, run_status, reason)`` (queue ``agents``) -- a finished / failed
  ``campaign_copy`` run -> drafts + checks + notifications (``results.finalize_from_run``).
* ``campaign_copy.purge`` (beat, daily, ``maintenance``) -- discarded / never-used drafts older than 180 days.
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "agents"
MAINTENANCE = "maintenance"


@celery_app.task(name="campaign_copy.finalize_run", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def finalize_run(tenant_id: str, run_id: str, run_status: str, reason: str | None) -> None:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> None:
        from app.services import campaign_copy_service

        async with tenant_session(tid) as db:
            await campaign_copy_service.finalize_from_run(db, uuid.UUID(str(run_id)), run_status, reason)

    run(job, tenant_id=tid)


@celery_app.task(name="campaign_copy.purge", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> int:
        from app.services import campaign_copy_service

        async with tenant_session(tid) as db:
            return await campaign_copy_service.purge(db)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


__all__ = ["finalize_run", "purge"]
