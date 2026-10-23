# ruff: file-ignore[line-too-long]
"""Job Network tasks (Epic 75, spec A3 / A5 / A8). Sync tasks on the ``ecosystem`` queue (AD-92); sweeps / flush /
purge on ``maintenance``."""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.tasks.runtime import run

SYNC_QUEUE = "ecosystem"
MAINT_QUEUE = "maintenance"


@celery_app.task(name="job_network.sync_posting", acks_late=True, queue=SYNC_QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def sync_posting(tenant_id: str, demand_id: str) -> str:
    async def job() -> str:
        from app.services.job_network import sync

        return await sync.sync_posting(uuid.UUID(tenant_id), uuid.UUID(demand_id))

    return str(run(job))


@celery_app.task(name="job_network.sync_posting_ref", acks_late=True, queue=SYNC_QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def sync_posting_ref(tenant_id: str, posting_ref: str) -> str:
    async def job() -> str:
        from app.services.job_network import sync

        return await sync.sync_posting_ref(uuid.UUID(tenant_id), uuid.UUID(posting_ref))

    return str(run(job))


@celery_app.task(name="job_network.sync_tenant", acks_late=True, queue=SYNC_QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def sync_tenant(tenant_id: str) -> int:
    async def job() -> int:
        from app.services.job_network import sync

        return await sync.sync_tenant(uuid.UUID(tenant_id))

    return int(run(job))


@celery_app.task(name="job_network.unlist_tenant", acks_late=True, queue=SYNC_QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def unlist_tenant(tenant_id: str) -> int:
    async def job() -> int:
        from app.services.job_network import sync

        return await sync.unlist_tenant(uuid.UUID(tenant_id))

    return int(run(job))


@celery_app.task(name="job_network.reconcile", queue=MAINT_QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def reconcile() -> Any:
    async def job() -> int:
        from app.services.job_network import sync

        return await sync.reconcile()

    return run(job)


@celery_app.task(name="job_network.flush_stats", queue=MAINT_QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def flush_stats() -> Any:
    async def job() -> int:
        from app.services.job_network import stats

        return await stats.flush_stats()

    return run(job)


@celery_app.task(name="job_network.purge", queue=MAINT_QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge() -> Any:
    async def job() -> int:
        from app.services.job_network import reports, sync

        removed = await sync.purge()
        await reports.purge_expired()
        return removed

    return run(job)
