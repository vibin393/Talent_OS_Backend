# ruff: noqa: E501
"""Demand Agent Celery tasks (Epic 55, A7). Every per-tenant task starts with ``tenant_id``.

* ``demand_intake.poll_mailboxes`` (beat, every 2 min, queue ``connectors``) -- per tenant with
  ``intake_mode=connected_mailbox``.
* ``demand_intake.purge_expired`` (beat, daily, ``maintenance``) -- raw content after 180 days.
* (``demand_insights.expire_flags`` lives in ``app.tasks.demand_insights``: the intake domain never imports insights.)
* ``demand_intake.finalize_run(tenant_id, message_id, run_id, run_status, reason)`` -- writes an intake run's
  outcome back to its message (from ``AgentRunFinished`` / ``AgentRunFailed``).

The agent runs themselves are on ``agents`` (runtime).
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "maintenance"
CONNECTORS = "connectors"


@celery_app.task(name="demand_intake.poll_mailboxes", queue=CONNECTORS)  # type: ignore[misc,untyped-decorator,unused-ignore]
def poll_mailboxes() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> int:
        from app.services.demand_intake import mailbox

        async with tenant_session(tid) as db:
            return await mailbox.poll_connected_mailbox(
                RequestContext(tenant_id=tid, actor_kind="system"), db
            )

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


@celery_app.task(name="demand_intake.purge_expired", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_expired() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> int:
        from app.services.demand_intake import items

        async with tenant_session(tid) as db:
            return await items.purge_expired(db)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


@celery_app.task(name="demand_intake.finalize_run", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def finalize_run(tenant_id: str, message_id: str, run_id: str, run_status: str, reason: str | None) -> None:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> None:
        from app.services.demand_intake import items

        async with tenant_session(tid) as db:
            await items.finalize_from_run(
                db,
                uuid.UUID(str(message_id)),
                run_id=uuid.UUID(str(run_id)),
                run_status=run_status,
                reason=reason,
            )

    run(job, tenant_id=tid)


__all__ = ["finalize_run", "poll_mailboxes", "purge_expired"]
