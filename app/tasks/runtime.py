"""Shared runtime of Celery tasks (AD-9).

``run(factory)`` executes an async job in its own event loop (the worker's, or a helper thread when a task
runs eagerly inside a request whose loop is busy) with a private NullPool engine
(``database.isolated_engine``) and a system request context. ``for_each_tenant`` iterates tenants by status
(suspended tenants are skipped by tenant beat jobs).
"""

import asyncio
import threading
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from app.core.context import RequestContext, reset_context, set_context
from app.core.database import isolated_engine, platform_session


def run(factory: Callable[[], Awaitable[Any]], *, tenant_id: uuid.UUID | None = None) -> Any:
    async def _job() -> Any:
        token = set_context(RequestContext(tenant_id=tenant_id))
        try:
            async with isolated_engine():
                return await factory()
        finally:
            reset_context(token)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_job())
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = asyncio.run(_job())
        except BaseException as exc:  # re-raised in the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=_target, name="talent-os-eager-task")
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


async def tenant_ids(statuses: tuple[str, ...] = ("active",)) -> list[uuid.UUID]:
    from app.services import identity_bridge

    async with platform_session() as pdb:
        return [t.id for t in await identity_bridge.list_tenants(pdb, statuses)]


async def for_each_tenant(
    job: Callable[[uuid.UUID], Awaitable[Any]], statuses: tuple[str, ...] = ("active",)
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for tid in await tenant_ids(statuses):
        token = set_context(RequestContext(tenant_id=tid))
        try:
            results[str(tid)] = await job(tid)
        except Exception:
            logger.exception("Tenant job {} failed for tenant {}", getattr(job, "__name__", job), tid)
            results[str(tid)] = "error"
        finally:
            reset_context(token)
    return results


def _eager(task: Any) -> bool:
    app = getattr(task, "app", None)
    conf = getattr(app, "conf", None)
    return bool(getattr(conf, "task_always_eager", False))


def enqueue(task: Any, *args: Any, **kwargs: Any) -> None:
    """``task.delay`` that never raises into the caller (after-commit handlers).

    Eager mode (Epic 13): called from an after-commit handler, the task is deferred until the committing
    session's ``commit()`` has returned (``core.events.flush_deferred``), so it never runs inside the commit.
    """
    from app.core import events

    if _eager(task) and events.defer_if_dispatching(task, args, dict(kwargs)):
        return
    try:
        task.delay(*args, **kwargs)
    except Exception:
        logger.exception("Could not enqueue {}", getattr(task, "name", task))
