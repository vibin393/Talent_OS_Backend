# ruff: file-ignore[line-too-long]
"""Marketplace tasks (Epic 69): ``marketplace.sync_platform_catalog`` (worker start + daily 02:40 IST, queue
``maintenance``: manifests -> listings, then every Tenant's re-consent pass), ``marketplace.health_sweep`` (hourly),
``marketplace.run_checks`` (automated checks of a partner version), ``marketplace.sync_failed`` (3 failed sync jobs ->
degraded) and ``marketplace.refresh_install_counts`` (daily aggregate)."""

import uuid

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "ecosystem"
MAINTENANCE = "maintenance"


async def _reconsent(tenant_id: uuid.UUID) -> int:
    from app.core.context import get_context
    from app.services.marketplace import reconsent

    async with tenant_session(tenant_id) as db:
        return await reconsent.apply_manifest_changes(get_context(), db)


@celery_app.task(name="marketplace.sync_platform_catalog", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def sync_platform_catalog() -> int:
    async def job() -> int:
        from app.services.marketplace import platform_catalog

        await platform_catalog.sync()
        results = await for_each_tenant(_reconsent)
        return sum(v for v in results.values() if isinstance(v, int))

    return int(run(job))


async def _sweep(tenant_id: uuid.UUID) -> int:
    from app.core.context import get_context
    from app.services.marketplace import health

    async with tenant_session(tenant_id) as db:
        return await health.sweep(get_context(), db)


@celery_app.task(name="marketplace.health_sweep")  # type: ignore[misc,untyped-decorator,unused-ignore]
def health_sweep() -> int:
    async def job() -> int:
        results = await for_each_tenant(_sweep)
        return sum(v for v in results.values() if isinstance(v, int))

    return int(run(job))


@celery_app.task(name="marketplace.run_checks")  # type: ignore[misc,untyped-decorator,unused-ignore]
def run_checks(version_id: str) -> int:
    async def job() -> int:
        from app.services.marketplace import submissions

        return len(await submissions.run_checks(uuid.UUID(str(version_id))))

    return int(run(job))


@celery_app.task(name="marketplace.sync_failed")  # type: ignore[misc,untyped-decorator,unused-ignore]
def sync_failed(
    tenant_id: str, installation_id: str, consecutive_failures: int, error_code: str | None
) -> bool:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> bool:
        from app.services.marketplace import health

        async with tenant_session(tid) as db:
            return await health.on_sync_failed(
                db, uuid.UUID(str(installation_id)), int(consecutive_failures), error_code
            )

    return bool(run(job, tenant_id=tid))


@celery_app.task(name="marketplace.refresh_install_counts", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def refresh_install_counts() -> int:
    async def job() -> int:
        from app.services.marketplace import review

        return await review.refresh_install_counts()

    return int(run(job))


__all__ = ["health_sweep", "refresh_install_counts", "run_checks", "sync_failed", "sync_platform_catalog"]
