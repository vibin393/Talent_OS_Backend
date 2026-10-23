# ruff: file-ignore[line-too-long]
"""Calendar Sync tasks (Epic 72, AD-92), queue ``ecosystem``. Every task starts with ``tenant_id`` (beats excepted).

* ``calendar_sync.ensure_channel(tenant_id, connection_id)`` / ``ensure_for_interview(tenant_id, interview_id)`` /
  ``backfill(tenant_id)`` -- create push channels.
* ``calendar_sync.delta(tenant_id, channel_id, full)`` -- one delta sync (coalesced by the ingress; a notification during
  the 30 s window queues one follow-up run, 30 s later).
* Beat: ``calendar_sync.renew_due_all`` (15 min), ``calendar_sync.poll_due_all`` (1 min); paused while the tenant's
  Ecosystem Kill Switch is on.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "ecosystem"


async def _in_tenant(tid: uuid.UUID, work: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    from app.services import connections_service, interviews_service  # ruff: ignore[unused-import] - wiring

    async with tenant_session(tid) as db:
        audit.set_actor(db, "system", None)
        return await work(db)


def _run(tenant_id: str, work: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    tid = uuid.UUID(str(tenant_id))
    return run(lambda: _in_tenant(tid, work), tenant_id=tid)


@celery_app.task(name="calendar_sync.ensure_channel", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def ensure_channel(tenant_id: str, connection_id: str) -> bool:
    from app.services import calendar_sync_service as svc

    async def work(db: AsyncSession) -> bool:
        return await svc.ensure_channel(db, uuid.UUID(str(connection_id))) is not None

    return bool(_run(tenant_id, work))


@celery_app.task(name="calendar_sync.ensure_for_interview", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def ensure_for_interview(tenant_id: str, interview_id: str) -> int:
    from app.services import calendar_sync_service as svc

    async def work(db: AsyncSession) -> int:
        return await svc.ensure_for_interview(db, uuid.UUID(str(interview_id)))

    return int(_run(tenant_id, work))


@celery_app.task(name="calendar_sync.backfill", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def backfill(tenant_id: str) -> int:
    from app.services import calendar_sync_service as svc

    return int(_run(tenant_id, svc.backfill))


async def _followup(channel_id: str) -> bool:
    from app.services.connections.store import get_store

    return await get_store().pop(f"calendar:delta:{channel_id}:followup") is not None


@celery_app.task(name="calendar_sync.delta", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def delta(tenant_id: str, channel_id: str, full: bool = False) -> str:
    from app.services import calendar_sync_service as svc

    async def work(db: AsyncSession) -> str:
        result = await svc.run_delta(db, uuid.UUID(str(channel_id)), full=full)
        return str(result.get("status"))

    status = str(_run(tenant_id, work))
    if run(lambda: _followup(channel_id), tenant_id=uuid.UUID(str(tenant_id))):
        if celery_app.conf.task_always_eager:
            FOLLOWUPS.append(channel_id)  # tests count the follow-up instead of sleeping 30 s
        else:
            try:
                delta.apply_async((tenant_id, channel_id, False), countdown=30, queue=QUEUE)
            except Exception:  # pragma: no cover - broker down
                logger.exception("Could not queue the calendar follow-up delta")
    return status


FOLLOWUPS: list[str] = []


async def _paused(tid: uuid.UUID) -> bool:
    from app.services import ecosystem_service

    return bool((await ecosystem_service.is_suspended(tid)).suspended)


@celery_app.task(name="calendar_sync.renew_due_all", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def renew_due_all() -> dict[str, Any]:
    from app.services import calendar_sync_service as svc

    async def job(tid: uuid.UUID) -> int:
        if await _paused(tid):
            return 0
        return int(await _in_tenant(tid, svc.renew_due))

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


@celery_app.task(name="calendar_sync.poll_due_all", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def poll_due_all() -> dict[str, Any]:
    from app.services import calendar_sync_service as svc

    async def job(tid: uuid.UUID) -> int:
        if await _paused(tid):
            return 0
        return int(await _in_tenant(tid, svc.poll_due))

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]
