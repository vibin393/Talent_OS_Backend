"""Onboarding Celery tasks (Epic 23). Every task starts with ``tenant_id`` (except fan-outs).

* ``onboarding.handle_event(tenant_id, name, payload)`` (queue ``interactive``): ``PlacementCreated``,
  ``PlacementDropped``, ``ComplianceRequirementsEvaluated``, ``Credential*``, ``SignatureRequest*``,
  ``TaskCompleted``, ``BankDetailsChangeDecided``. Idempotent (AD-9).
* ``onboarding.reminders_all`` (beat, daily) -> ``onboarding.reminders(tenant_id)`` (queue ``maintenance``).
"""

import uuid
from typing import Any

from loguru import logger

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "maintenance"


async def _handle(tenant_id: uuid.UUID, name: str, payload: dict[str, Any]) -> Any:
    from app.services import onboarding

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    try:
        async with tenant_session(tenant_id) as db:
            return await onboarding.handle_event(ctx, db, name, payload)
    except Exception:
        logger.exception("onboarding handler {} failed", name)
        raise


@celery_app.task(name="onboarding.handle_event", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def handle_event(tenant_id: str, name: str, payload: dict[str, Any]) -> Any:
    tid = uuid.UUID(str(tenant_id))
    return run(lambda: _handle(tid, name, payload), tenant_id=tid)


async def _reminders(tenant_id: uuid.UUID) -> int:
    from app.services import onboarding

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        return await onboarding.send_reminders(ctx, db)


@celery_app.task(name="onboarding.reminders", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def reminders(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _reminders(tid), tenant_id=tid))


@celery_app.task(name="onboarding.reminders_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def reminders_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(reminders, str(tid))
    return len(ids)
