"""Candidate Celery tasks (AD-9): ``process_import_item(tenant_id, item_id)``.

Each run opens its own tenant-scoped session on a private (NullPool) engine inside its own event loop, so it
works both in a worker process and eagerly (``CELERY_TASK_ALWAYS_EAGER=true``) from an after-commit handler
of a request -- then it runs in a helper thread, since the request's loop is already running.
Idempotent: items that are not queued/processing are skipped; a crash rolls the item back to queued.
"""

import asyncio
import threading
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.celery import celery_app
from app.core.context import RequestContext, reset_context, set_context
from app.core.database import ORG_ID_KEY, ORG_KIND_KEY, TENANT_INFO_KEY
from app.core.permissions import Principal
from app.core.settings import get_settings

QUEUE = "interactive"  # Epic 13 (AD-37): the queue of every task in this module


def _run_coroutine(factory: Callable[[], Awaitable[Any]]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_as_coro(factory))
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = asyncio.run(_as_coro(factory))
        except BaseException as exc:  # re-raised in the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=_target, name="talent-os-eager-task")
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


async def _as_coro(factory: Callable[[], Awaitable[Any]]) -> Any:
    return await factory()


async def _in_tenant_session(tenant_id: uuid.UUID, work: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    engine = create_async_engine(get_settings().app_db_url(), poolclass=NullPool)
    try:
        session = AsyncSession(
            engine,
            expire_on_commit=False,
            info={TENANT_INFO_KEY: str(tenant_id), ORG_KIND_KEY: "internal", ORG_ID_KEY: ""},
        )
        try:
            result = await work(session)
            await session.commit()
            return result
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()
    finally:
        await engine.dispose()


async def _process(tenant_id: uuid.UUID, item_id: uuid.UUID) -> str | None:
    from app.models.candidates import ImportBatch, ImportItem
    from app.services.candidates import imports

    async def _owner(db: AsyncSession) -> uuid.UUID | None:
        item = await db.get(ImportItem, item_id)
        batch = await db.get(ImportBatch, item.batch_id) if item is not None else None
        return batch.created_by if batch is not None else None

    owner = await _in_tenant_session(tenant_id, _owner)
    principal = Principal(type="user", id=owner or uuid.UUID(int=0), tenant_id=tenant_id)
    token = set_context(RequestContext(tenant_id=tenant_id, principal=principal))
    ctx = RequestContext(tenant_id=tenant_id, principal=principal)
    try:
        try:
            return await _in_tenant_session(tenant_id, lambda db: imports.process_item(ctx, db, item_id))  # type: ignore[no-any-return]
        except Exception:
            logger.exception("Import item {} failed during processing", item_id)
            await _in_tenant_session(
                tenant_id, lambda db: imports.mark_failed(ctx, db, item_id, "processing_error")
            )
            return "failed"
    finally:
        reset_context(token)


async def _fair_process(tenant_id: uuid.UUID, item_id: uuid.UUID) -> str | None:
    """Epic 12 (NFR-16): hold one of the tenant's bulk slots while processing."""
    from app.core import fairness

    async with fairness.tenant_slot(tenant_id, "import"):
        return await _process(tenant_id, item_id)


@celery_app.task(name="candidates.process_import_item", acks_late=True, bind=True, max_retries=None)  # type: ignore[misc,untyped-decorator,unused-ignore]
def process_import_item(self: Any, tenant_id: str, item_id: str) -> str | None:
    from app.core import fairness

    tid, iid = uuid.UUID(tenant_id), uuid.UUID(item_id)
    try:
        return _run_coroutine(lambda: _fair_process(tid, iid))  # type: ignore[no-any-return]
    except fairness.SlotUnavailable as exc:
        raise self.retry(countdown=fairness.retry_countdown(self.request.retries), exc=exc) from exc
