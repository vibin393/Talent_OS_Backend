"""Offer Celery tasks (AD-9). Each task takes ``tenant_id`` first, runs in its own tenant session on a private
NullPool engine (a helper thread when a loop is already running, e.g. eager mode) and is idempotent.

* ``offers.render_offer_letter(tenant_id, offer_id, version_no)`` -- letter PDF (skips when up to date).
* ``offers.send_offer_email(tenant_id, offer_id, kind)`` -- released / updated / withdrawn / owner notices.
* ``offers.finalize_accepted_offer(tenant_id, offer_id)`` -- signed PDF with the acceptance certificate.
* ``offers.ensure_defaults(tenant_id)`` -- default letter templates (tenant provisioning).
* ``offers.expire_due`` (beat, every 15 min) / ``offers.expiry_warnings`` (beat, hourly) fan out per active
  tenant to ``offers.expire_due_tenant`` / ``offers.expiry_warnings_tenant``.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.tasks.pipeline import in_tenant, run_sync

QUEUE = "interactive"  # Epic 13 (AD-37): the queue of every task in this module


def _uid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


@celery_app.task(name="offers.render_offer_letter", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def render_offer_letter(tenant_id: str, offer_id: str, version_no: int) -> bool:
    async def _work(ctx: RequestContext, db: AsyncSession) -> bool:
        from app.services.offers import notifications

        return await notifications.render_letter(ctx, db, _uid(offer_id), int(version_no))

    return bool(run_sync(lambda: in_tenant(_uid(tenant_id), _work)))


@celery_app.task(name="offers.send_offer_email", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def send_offer_email(tenant_id: str, offer_id: str, kind: str) -> bool:
    async def _work(ctx: RequestContext, db: AsyncSession) -> bool:
        from app.services.offers import notifications

        return await notifications.send_offer_email(ctx, db, _uid(offer_id), kind)

    return bool(run_sync(lambda: in_tenant(_uid(tenant_id), _work)))


@celery_app.task(name="offers.finalize_accepted_offer", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def finalize_accepted_offer(tenant_id: str, offer_id: str) -> bool:
    async def _work(ctx: RequestContext, db: AsyncSession) -> bool:
        from app.services.offers import notifications

        return await notifications.finalize_accepted(ctx, db, _uid(offer_id))

    return bool(run_sync(lambda: in_tenant(_uid(tenant_id), _work)))


@celery_app.task(name="offers.ensure_defaults", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def ensure_defaults(tenant_id: str) -> int:
    async def _work(ctx: RequestContext, db: AsyncSession) -> int:  # ruff: ignore[unused-function-argument]
        from app.services.offers import templates

        return await templates.ensure_default_templates(db, _uid(tenant_id))

    return int(run_sync(lambda: in_tenant(_uid(tenant_id), _work)))


@celery_app.task(name="offers.expire_due_tenant", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def expire_due_tenant(tenant_id: str) -> int:
    async def _work(ctx: RequestContext, db: AsyncSession) -> int:
        from app.services.offers import expiry

        return await expiry.expire_due(ctx, db)

    return int(run_sync(lambda: in_tenant(_uid(tenant_id), _work)))


@celery_app.task(name="offers.expiry_warnings_tenant", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def expiry_warnings_tenant(tenant_id: str) -> int:
    async def _work(ctx: RequestContext, db: AsyncSession) -> int:
        from app.services.offers import notifications

        return await notifications.send_expiry_warnings(ctx, db)

    return int(run_sync(lambda: in_tenant(_uid(tenant_id), _work)))


def _fan_out(task_name: str) -> int:
    from app.tasks.runtime import run, tenant_ids

    ids = run(tenant_ids)
    for tid in ids:
        try:
            celery_app.tasks[task_name].delay(str(tid))
        except Exception:
            logger.exception("Could not enqueue {} for tenant {}", task_name, tid)
    return len(ids)


@celery_app.task(name="offers.expire_due")  # type: ignore[misc,untyped-decorator,unused-ignore]
def expire_due() -> int:
    """Beat (every 15 minutes): one expiry run per active tenant."""
    return _fan_out("offers.expire_due_tenant")


@celery_app.task(name="offers.expiry_warnings")  # type: ignore[misc,untyped-decorator,unused-ignore]
def expiry_warnings() -> int:
    """Beat (hourly): 24 h expiry warnings per active tenant."""
    return _fan_out("offers.expiry_warnings_tenant")
