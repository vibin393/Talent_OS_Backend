"""Documents & eSignature Celery tasks (Epic 22, AD-9). Each task takes ``tenant_id`` first, runs in its own
tenant session and is idempotent.

* ``documents.notify_signer(tenant_id, signer_id, reminder)`` -- single-use link + email (+ in-app copy).
* ``documents.complete_request(tenant_id, request_id)`` -- certificate, final PDF, ``SignatureRequestCompleted``.
* ``documents.notify_declined(tenant_id, request_id)`` -- tells the requester.
* Beat: ``documents.esign_reminders`` (hourly), ``documents.esign_expiry`` (every 15 min),
  ``documents.purge_otps`` (daily) fan out per active tenant.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery import celery_app
from app.core.context import RequestContext

QUEUE = "interactive"  # Epic 13 (AD-37)


def _uid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


def _go(tenant_id: str, work: Any) -> Any:
    """Run ``work(ctx, db)`` in an internal-org tenant session (``tenant_session``: org RLS needs the scope)."""
    from app.core import audit
    from app.core.database import tenant_session
    from app.tasks.runtime import run

    tid = _uid(tenant_id)

    async def _job() -> Any:
        async with tenant_session(tid) as db:
            audit.set_actor(db, "system", None)
            return await work(RequestContext(tenant_id=tid), db)

    return run(_job, tenant_id=tid)


@celery_app.task(name="documents.notify_signer", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def notify_signer(tenant_id: str, signer_id: str, reminder: bool = False) -> bool:
    async def _work(ctx: RequestContext, db: AsyncSession) -> bool:
        from app.services.documents import esign

        return await esign.notify_signer(ctx, db, _uid(signer_id), reminder=bool(reminder))

    return bool(_go(tenant_id, _work))


@celery_app.task(name="documents.complete_request", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def complete_request(tenant_id: str, request_id: str) -> bool:
    async def _work(ctx: RequestContext, db: AsyncSession) -> bool:
        from app.services.documents import esign

        done = await esign.complete_request(ctx, db, _uid(request_id))
        if done is not None:
            req = await esign.load_request(db, _uid(request_id))
            if req.subject_kind != "offer":
                await esign.notify_completed_owner(db, req)
        return done is not None

    return bool(_go(tenant_id, _work))


@celery_app.task(name="documents.notify_declined", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def notify_declined(tenant_id: str, request_id: str) -> bool:
    async def _work(ctx: RequestContext, db: AsyncSession) -> bool:
        from app.services.documents import esign

        return await esign.notify_declined(ctx, db, _uid(request_id))

    return bool(_go(tenant_id, _work))


@celery_app.task(name="documents.esign_reminders_tenant", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def esign_reminders_tenant(tenant_id: str) -> int:
    async def _work(ctx: RequestContext, db: AsyncSession) -> int:
        from app.services.documents import esign

        return await esign.send_reminders(ctx, db)

    return int(_go(tenant_id, _work))


@celery_app.task(name="documents.esign_expiry_tenant", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def esign_expiry_tenant(tenant_id: str) -> int:
    async def _work(ctx: RequestContext, db: AsyncSession) -> int:
        from app.services.documents import esign

        return await esign.expire_due(ctx, db)

    return int(_go(tenant_id, _work))


@celery_app.task(name="documents.purge_otps_tenant", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_otps_tenant(tenant_id: str) -> int:
    async def _work(ctx: RequestContext, db: AsyncSession) -> int:  # noqa: ARG001
        from app.services.documents import esign

        return await esign.purge_otps(db)

    return int(_go(tenant_id, _work))


def _fan_out(task_name: str) -> int:
    from app.tasks.runtime import run, tenant_ids

    ids = run(tenant_ids)
    for tid in ids:
        try:
            celery_app.tasks[task_name].delay(str(tid))
        except Exception:
            logger.exception("Could not enqueue {} for tenant {}", task_name, tid)
    return len(ids)


@celery_app.task(name="documents.esign_reminders")  # type: ignore[misc,untyped-decorator,unused-ignore]
def esign_reminders() -> int:
    return _fan_out("documents.esign_reminders_tenant")


@celery_app.task(name="documents.esign_expiry")  # type: ignore[misc,untyped-decorator,unused-ignore]
def esign_expiry() -> int:
    return _fan_out("documents.esign_expiry_tenant")


@celery_app.task(name="documents.purge_otps")  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_otps() -> int:
    return _fan_out("documents.purge_otps_tenant")
