"""Demand Celery tasks (AD-9): SLA evaluation.

* ``demands.tasks.evaluate_slas(tenant_id)`` recomputes the tenant's open SLA clocks inside its tenant
  session; idempotent (events only when a status differs from ``last_notified_status``).
* ``demands.tasks.evaluate_slas_all`` (beat, every 5 min) enqueues one evaluation per active tenant.

Each run goes through ``app.tasks.runtime.run``: its own event loop (a helper thread when a loop is already
running, e.g. eager mode from a request) with a private engine, so it never borrows a pooled connection that
belongs to another thread's loop (``--pool=threads``).
"""

import uuid

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.celery import celery_app
from app.core.context import RequestContext, reset_context, set_context
from app.core.database import ORG_ID_KEY, ORG_KIND_KEY, TENANT_INFO_KEY
from app.core.settings import get_settings
from app.tasks.runtime import run

QUEUE = "interactive"  # Epic 13 (AD-37): the queue of every task in this module


async def evaluate_slas_async(tenant_id: uuid.UUID) -> int:
    from app.services.demands import sla

    ctx = RequestContext(tenant_id=tenant_id)
    token = set_context(ctx)
    engine = create_async_engine(get_settings().app_db_url(), poolclass=NullPool)
    try:
        session = AsyncSession(
            engine,
            expire_on_commit=False,
            info={TENANT_INFO_KEY: str(tenant_id), ORG_KIND_KEY: "internal", ORG_ID_KEY: ""},
        )
        try:
            emitted = await sla.evaluate_tenant(ctx, session)
            await session.commit()
            return emitted
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()
    finally:
        await engine.dispose()
        reset_context(token)


@celery_app.task(name="demands.tasks.evaluate_slas", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def evaluate_slas(tenant_id: str) -> int:
    return int(run(lambda: evaluate_slas_async(uuid.UUID(str(tenant_id)))))


@celery_app.task(name="demands.tasks.evaluate_slas_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def evaluate_slas_all() -> int:
    from app.services import directory_service as directory

    tenant_ids: list[uuid.UUID] = run(directory.active_tenant_ids)
    for tenant_id in tenant_ids:
        try:
            evaluate_slas.delay(str(tenant_id))
        except Exception:
            logger.exception("Could not enqueue SLA evaluation for tenant {}", tenant_id)
    return len(tenant_ids)
