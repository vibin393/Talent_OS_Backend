# ruff: file-ignore[line-too-long]
"""Webhook tasks (Epic 68, AD-79/AD-92), queue ``webhooks`` (worker ``worker-webhooks``, egress-only network).

* ``webhooks.pump(tenant_id)`` -- after-commit kick (deduplicated 1 s) and ``webhooks.pump_all`` (beat, 5 s);
* ``webhooks.deliver(tenant_id, delivery_id)`` -- one attempt;
* ``webhooks.verify(tenant_id, subscription_id)`` -- the handshake (automatic retries 1 min / 10 min / 1 h);
* ``webhooks.health_scan`` (beat, 5 min); ``webhooks.replay_range``; ``webhooks.on_installation_changed``;
* ``webhooks.purge`` (daily, queue ``maintenance``).
"""

import uuid
from datetime import datetime
from typing import Any

from app.core.celery import celery_app
from app.tasks.runtime import enqueue, for_each_tenant, run

QUEUE = "webhooks"


def _eager(task: Any) -> bool:
    return bool(getattr(getattr(getattr(task, "app", None), "conf", None), "task_always_eager", False))


@celery_app.task(name="webhooks.pump", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def pump(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> list[uuid.UUID]:
        from app.services import webhooks_service

        return await webhooks_service.pump(tid)

    ids = run(job, tenant_id=tid) or []
    for delivery_id in ids:
        enqueue(deliver, str(tid), str(delivery_id))
    return len(ids)


@celery_app.task(name="webhooks.pump_all", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def pump_all() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> bool:
        from app.services import webhooks_service

        return await webhooks_service.has_due(tid)

    due = run(lambda: for_each_tenant(job)) or {}
    for tid, flag in due.items():
        if flag is True:
            enqueue(pump, tid)
    return due  # type: ignore[no-any-return]


@celery_app.task(name="webhooks.deliver", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def deliver(tenant_id: str, delivery_id: str) -> str | None:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> str | None:
        from app.services import webhooks_service

        return await webhooks_service.deliver(tid, uuid.UUID(str(delivery_id)))

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="webhooks.verify", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def verify(tenant_id: str, subscription_id: str) -> int | None:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int | None:
        from app.services import webhooks_service

        return await webhooks_service.verify(tid, uuid.UUID(str(subscription_id)))

    delay = run(job, tenant_id=tid)
    if delay is not None and not _eager(verify):  # eager (tests / dev): no automatic re-run loop
        verify.apply_async(args=[str(tid), str(subscription_id)], countdown=int(delay))
    return delay  # type: ignore[no-any-return]


@celery_app.task(name="webhooks.health_scan", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def health_scan() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> int:
        from app.services import webhooks_service

        return await webhooks_service.health_scan(tid)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


@celery_app.task(name="webhooks.replay_range", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def replay_range(tenant_id: str, subscription_id: str, date_from: str, date_to: str, statuses: list[str], user_id: str = "") -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services import webhooks_service

        return await webhooks_service.replay_range_task(
            tid, uuid.UUID(subscription_id), datetime.fromisoformat(date_from), datetime.fromisoformat(date_to), list(statuses),
            uuid.UUID(user_id) if user_id else None,
        )  # fmt: skip

    return int(run(job, tenant_id=tid) or 0)


@celery_app.task(name="webhooks.on_installation_changed", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def on_installation_changed(tenant_id: str, installation_id: str, change: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services import webhooks_service

        return await webhooks_service.on_installation_changed(tid, uuid.UUID(installation_id), change)

    return int(run(job, tenant_id=tid) or 0)


@celery_app.task(name="webhooks.purge", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> dict[str, int]:
        from app.services import webhooks_service

        return await webhooks_service.purge(tid)

    return run(lambda: for_each_tenant(job, ("active", "suspended", "offboarding")))  # type: ignore[no-any-return]


__all__ = ["deliver", "health_scan", "on_installation_changed", "pump", "pump_all", "purge", "replay_range", "verify"]
