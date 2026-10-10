"""Epic 33 Celery tasks. Every task starts with ``tenant_id`` (fan-out beats take an optional one).

* ``comms.sync_replies`` (beat, every 2 min, queue ``connectors``): FR-171 reply capture per tenant.
* ``messaging.release_deferred`` (beat, every 5 min, ``connectors``): SMS deferred by quiet hours.
* ``messaging.job_failed(tenant_id, message_id)`` (``connectors``): a send job used every attempt.
* ``platform.check_custom_domains`` (beat, every 10 min, queue ``maintenance``): DNS TXT verification.
"""

import uuid

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.ops_runtime import for_each_tenant, run

QUEUE = "connectors"  # Epic 13 (AD-37); ``platform.check_custom_domains`` overrides it per task


@celery_app.task(name="comms.sync_replies")  # type: ignore[untyped-decorator,unused-ignore]
def sync_replies(tenant_id: str | None = None) -> dict[str, object]:
    from app.services.comms import inbound

    async def one(tid: uuid.UUID) -> dict[str, int]:
        async with tenant_session(tid) as db:
            return await inbound.sync_threads(RequestContext(tenant_id=tid, actor_kind="system"), db)

    return run(lambda: for_each_tenant(one, ("active",), only=tenant_id))  # type: ignore[no-any-return]


@celery_app.task(name="messaging.release_deferred")  # type: ignore[untyped-decorator,unused-ignore]
def release_deferred(tenant_id: str | None = None) -> dict[str, object]:
    from app.services.messaging import sending

    async def one(tid: uuid.UUID) -> int:
        async with tenant_session(tid) as db:
            return await sending.release_deferred(RequestContext(tenant_id=tid, actor_kind="system"), db)

    return run(lambda: for_each_tenant(one, ("active",), only=tenant_id))  # type: ignore[no-any-return]


@celery_app.task(name="messaging.job_failed", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def job_failed(tenant_id: str, message_id: str) -> bool:
    from app.services.messaging import sending

    tid = uuid.UUID(str(tenant_id))

    async def job() -> bool:
        async with tenant_session(tid) as db:
            return await sending.job_failed(
                RequestContext(tenant_id=tid, actor_kind="system"), db, uuid.UUID(str(message_id))
            )

    return bool(run(job))


@celery_app.task(name="platform.check_custom_domains", queue="maintenance")  # type: ignore[untyped-decorator,unused-ignore]
def check_custom_domains() -> dict[str, int]:
    from app.services import custom_domains

    return run(custom_domains.check_domains)  # type: ignore[no-any-return]
