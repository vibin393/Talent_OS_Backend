"""Runtime shared by the Epic 12 Celery tasks (privacy, platform, maintenance; AD-9).

``run(factory)`` executes an async job in its own event loop (the worker's, or a helper thread when a task
runs eagerly inside a request whose loop is busy) with a private NullPool engine
(``database.isolated_engine``: every ``tenant_session`` / ``platform_session`` inside uses it) and a system
request context. ``for_each_tenant`` iterates tenants by status (suspended tenants are skipped by tenant
beat jobs).
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from app.core.context import RequestContext, reset_context, set_context
from app.core.database import platform_session
from app.tasks.runtime import run

__all__ = ["enqueue", "for_each_tenant", "run", "tenant_ids"]


async def tenant_ids(statuses: tuple[str, ...] = ("active",)) -> list[uuid.UUID]:
    from app.services import identity_bridge

    async with platform_session() as pdb:
        return [t.id for t in await identity_bridge.list_tenants(pdb, statuses)]


async def for_each_tenant(
    job: Callable[[uuid.UUID], Awaitable[Any]],
    statuses: tuple[str, ...] = ("active",),
    only: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    """Run ``job`` per tenant (``only``: a single tenant -- ops re-runs and tests)."""
    results: dict[str, Any] = {}
    ids = [uuid.UUID(str(only))] if only is not None else await tenant_ids(statuses)
    for tid in ids:
        token = set_context(RequestContext(tenant_id=tid))
        try:
            results[str(tid)] = await job(tid)
        except Exception:
            logger.exception("Tenant job {} failed for tenant {}", getattr(job, "__name__", job), tid)
            results[str(tid)] = "error"
        finally:
            reset_context(token)
    return results


def enqueue(task: Any, *args: Any, **kwargs: Any) -> None:
    """``task.delay`` that never raises into the caller (after-commit handlers)."""
    try:
        task.delay(*args, **kwargs)
    except Exception:
        logger.exception("Could not enqueue {}", getattr(task, "name", task))
