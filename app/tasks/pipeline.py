"""Pipeline Celery tasks (AD-9). Each task takes ``tenant_id`` first, runs in its own tenant session on a
private NullPool engine (a helper thread when a loop is already running, e.g. eager mode) and is idempotent.

* ``pipeline.tasks.ensure_defaults(tenant_id)`` -- default stages / reason codes / questionnaire.
* ``pipeline.tasks.deliver_ai_screening_invite(tenant_id, screening_id)`` -- link + email to the candidate.
* ``pipeline.tasks.send_submission_emails(tenant_id, submission_id)`` -- one email per recipient contact.
* ``pipeline.tasks.send_rejection_email(tenant_id, application_id)`` -- tenant rejection template.

The AI summary task lives in ``app.tasks.pipeline_ai`` (it must not import any human-only function).
"""

import asyncio
import threading
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import audit, tenant_registry
from app.core.celery import celery_app
from app.core.context import RequestContext, reset_context, set_context
from app.core.database import ORG_ID_KEY, ORG_KIND_KEY, TENANT_INFO_KEY, _schema_connect_args
from app.core.settings import get_settings

QUEUE = "interactive"  # Epic 13 (AD-37): the queue of every task in this module


def run_sync(factory: Callable[[], Awaitable[Any]]) -> Any:
    async def _coro() -> Any:
        return await factory()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_coro())
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = asyncio.run(_coro())
        except BaseException as exc:  # re-raised in the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=_target, name="talent-os-pipeline-task")
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


async def in_tenant(
    tenant_id: uuid.UUID,
    work: Callable[[RequestContext, AsyncSession], Awaitable[Any]],
    *,
    actor: str = "system",
) -> Any:
    ctx = RequestContext(tenant_id=tenant_id)
    token = set_context(ctx)
    # Route to the tenant's OWN database + schema (silo / schema-per-app), mirroring the web request path
    # (app.core.database._resolve_maker). Without this a task would hit the pooled app DB and never see a
    # company-DB tenant's rows (offer-letter render etc. silently no-op). Fall back to the pooled DB when the
    # tenant has no registry row (pooled mode) or resolution is unavailable.
    try:
        db_name, schema_name = await tenant_registry.resolve_db_target(tenant_id)
        engine = create_async_engine(
            get_settings().app_db_url(db_name),
            poolclass=NullPool,
            connect_args=_schema_connect_args(schema_name),
        )
    except Exception:
        engine = create_async_engine(get_settings().app_db_url(), poolclass=NullPool)
    try:
        session = AsyncSession(
            engine,
            expire_on_commit=False,
            info={TENANT_INFO_KEY: str(tenant_id), ORG_KIND_KEY: "internal", ORG_ID_KEY: ""},
        )
        try:
            audit.set_actor(session, actor, None)
            result = await work(ctx, session)
            await session.commit()
            return result
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()
    finally:
        await engine.dispose()
        reset_context(token)


@celery_app.task(name="pipeline.tasks.ensure_defaults", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def ensure_defaults(tenant_id: str) -> bool:
    async def _work(ctx: RequestContext, db: AsyncSession) -> bool:  # ruff: ignore[unused-function-argument]
        from app.services.pipeline import screening, stages

        await stages.ensure_default_stages(db, uuid.UUID(tenant_id))
        await screening.ensure_default_questionnaire(db)
        return True

    return bool(run_sync(lambda: in_tenant(uuid.UUID(str(tenant_id)), _work)))


@celery_app.task(name="pipeline.tasks.deliver_ai_screening_invite", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def deliver_ai_screening_invite(tenant_id: str, screening_id: str) -> bool:
    async def _work(ctx: RequestContext, db: AsyncSession) -> bool:
        from app.services.pipeline import screening

        return await screening.deliver_invite(ctx, db, uuid.UUID(str(screening_id)))

    return bool(run_sync(lambda: in_tenant(uuid.UUID(str(tenant_id)), _work)))


@celery_app.task(name="pipeline.tasks.send_submission_emails", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def send_submission_emails(tenant_id: str, submission_id: str) -> int:
    async def _work(ctx: RequestContext, db: AsyncSession) -> int:
        from app.services.pipeline import submissions

        return await submissions.deliver_emails(ctx, db, uuid.UUID(str(submission_id)))

    return int(run_sync(lambda: in_tenant(uuid.UUID(str(tenant_id)), _work)))


@celery_app.task(name="pipeline.tasks.send_rejection_email", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def send_rejection_email(tenant_id: str, application_id: str) -> bool:
    async def _work(ctx: RequestContext, db: AsyncSession) -> bool:
        from app.services.pipeline import notifications

        return await notifications.send_rejection_email(ctx, db, uuid.UUID(str(application_id)))

    return bool(run_sync(lambda: in_tenant(uuid.UUID(str(tenant_id)), _work)))
