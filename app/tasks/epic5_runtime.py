"""Shared runtime for the Epic 5 Celery tasks (search, ai embeddings, matching).

Tasks run their coroutine on a long-lived background event loop (one daemon thread per process) with a small
pooled engine for the runtime role, so repeated tasks reuse DB connections instead of opening one per task.
This matters in eager mode (dev / tests), where every candidate write runs its refresh tasks inline from the
after-commit handler of the request. A task started *from* the background loop (a nested eager task, e.g.
``MatchRunRequested`` dispatched after a run row commits) runs in a fresh thread + loop with a private
NullPool engine instead, so it never waits on its own loop. Every session is tenant-scoped (AD-9).
"""

import asyncio
import threading
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.context import RequestContext, reset_context, set_context
from app.core.database import ORG_ID_KEY, ORG_KIND_KEY, TENANT_INFO_KEY
from app.core.settings import get_settings

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_engine: AsyncEngine | None = None
# Marks threads that are executing an Epic 5 task (the background loop thread or a fresh task thread): a task
# started from one of them (nested eager dispatch) must never wait on the background loop, which may be the
# very thread that is blocked waiting for it.
_worker = threading.local()


def _background_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread  # process-wide runtime, created lazily (after a worker fork)
    with _lock:
        if _loop is None or _thread is None or not _thread.is_alive():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name="talent-os-epic5-loop", daemon=True)
            thread.start()
            _loop, _thread = loop, thread
        return _loop


def _run_in_fresh_thread(factory: Callable[[], Awaitable[Any]]) -> Any:
    box: dict[str, Any] = {}

    async def _coro() -> Any:
        return await factory()

    def _target() -> None:
        _worker.active = True
        try:
            box["result"] = asyncio.run(_coro())
        except BaseException as exc:  # re-raised in the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=_target, name="talent-os-epic5-task")
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def run_sync(factory: Callable[[], Awaitable[Any]]) -> Any:
    """Run ``factory()`` to completion from synchronous code (a Celery task body)."""
    if threading.current_thread() is _thread or getattr(_worker, "active", False):
        return _run_in_fresh_thread(factory)
    loop = _background_loop()

    async def _coro() -> Any:
        return await factory()

    return asyncio.run_coroutine_threadsafe(_coro(), loop).result()


def _shared_engine() -> AsyncEngine:
    global _engine  # bound to the background loop; used only from it
    if _engine is None:
        _engine = create_async_engine(
            get_settings().app_db_url(), pool_size=4, max_overflow=4, pool_pre_ping=True, pool_recycle=1800
        )
    return _engine


async def in_tenant_session(
    tenant_id: uuid.UUID, work: Callable[[RequestContext, AsyncSession], Awaitable[Any]]
) -> Any:
    """Run ``work(ctx, db)`` in a committed tenant session with a system request context."""
    ctx = RequestContext(tenant_id=tenant_id)
    token = set_context(ctx)
    on_background = _loop is not None and asyncio.get_running_loop() is _loop
    engine = (
        _shared_engine()
        if on_background
        else create_async_engine(get_settings().app_db_url(), poolclass=NullPool)
    )
    try:
        session = AsyncSession(
            engine,
            expire_on_commit=False,
            info={TENANT_INFO_KEY: str(tenant_id), ORG_KIND_KEY: "internal", ORG_ID_KEY: ""},
        )
        try:
            result = await work(ctx, session)
            await session.commit()
            return result
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()
    finally:
        if not on_background:
            await engine.dispose()
        reset_context(token)


def is_eager() -> bool:
    return get_settings().celery_task_always_eager


def uuids(values: Any) -> list[uuid.UUID]:
    return [uuid.UUID(str(v)) for v in (values or [])]
