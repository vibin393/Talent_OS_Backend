# ruff: file-ignore[line-too-long]
"""Developer Portal tasks (Epic 74, spec layout): ``developer.create_sandbox`` / ``reset_sandbox`` / ``delete_sandbox``
and ``developer.finalize_conformance`` on ``ecosystem``; ``developer.purge_unverified``, ``developer.expire_invitations``,
``developer.expire_verifications`` and ``developer.expire_conformance_runs`` on ``maintenance`` (daily / hourly beat)."""

import uuid

from app.core.celery import celery_app
from app.tasks.runtime import run

QUEUE = "ecosystem"
MAINTENANCE = "maintenance"


@celery_app.task(name="developer.create_sandbox")  # type: ignore[misc,untyped-decorator,unused-ignore]
def create_sandbox(sandbox_id: str) -> None:
    async def job() -> None:
        from app.services.developer_portal import sandboxes

        await sandboxes.run_create(uuid.UUID(str(sandbox_id)))

    run(job)


@celery_app.task(name="developer.reset_sandbox")  # type: ignore[misc,untyped-decorator,unused-ignore]
def reset_sandbox(sandbox_id: str, reinstall_apps: bool = False) -> None:
    async def job() -> None:
        from app.services.developer_portal import sandboxes

        await sandboxes.run_reset(uuid.UUID(str(sandbox_id)), reinstall_apps=bool(reinstall_apps))

    run(job)


@celery_app.task(name="developer.delete_sandbox")  # type: ignore[misc,untyped-decorator,unused-ignore]
def delete_sandbox(sandbox_id: str) -> None:
    async def job() -> None:
        from app.services.developer_portal import sandboxes

        await sandboxes.run_delete(uuid.UUID(str(sandbox_id)))

    run(job)


@celery_app.task(name="developer.finalize_conformance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def finalize_conformance(run_id: str) -> None:
    async def job() -> None:
        from app.services.developer_portal.conformance import runner

        await runner.finalize_run(uuid.UUID(str(run_id)))

    run(job)


@celery_app.task(name="developer.purge_unverified", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_unverified() -> int:
    async def job() -> int:
        from app.services import developer_portal_service

        return await developer_portal_service.purge_unverified()

    return int(run(job))


@celery_app.task(name="developer.expire_invitations", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def expire_invitations() -> int:
    async def job() -> int:
        from app.services import developer_portal_service

        return await developer_portal_service.expire_invitations()

    return int(run(job))


@celery_app.task(name="developer.expire_verifications", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def expire_verifications() -> int:
    async def job() -> int:
        from app.services.developer_portal import verification

        return await verification.expire_verifications()

    return int(run(job))


@celery_app.task(name="developer.expire_conformance_runs", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def expire_conformance_runs() -> int:
    async def job() -> int:
        from app.services.developer_portal.conformance import runner

        return await runner.expire_runs()

    return int(run(job))
