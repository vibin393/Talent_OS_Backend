# ruff: file-ignore[line-too-long]
"""Portal Copilot Celery tasks (Epic 77). Every task starts with ``tenant_id`` (beat fan-outs excepted).

* queue ``conversations``: ``portal_copilot.on_run_finished`` (store the answer, publish ``turn.completed|failed``),
  ``portal_copilot.on_draft_decided`` (event message);
* queue ``maintenance`` (daily 03:05 UTC): ``portal_copilot.purge_expired`` -- conversations past ``expires_at``.
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.tasks.runtime import for_each_tenant, run

QUEUE = "conversations"
MAINTENANCE = "maintenance"


@celery_app.task(name="portal_copilot.on_run_finished", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def on_run_finished(tenant_id: str, run_id: str) -> bool:
    from app.services import portal_copilot_service

    tid = uuid.UUID(str(tenant_id))
    return bool(run(lambda: portal_copilot_service.record_answer(tid, uuid.UUID(str(run_id))), tenant_id=tid))


@celery_app.task(name="portal_copilot.on_draft_decided", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def on_draft_decided(tenant_id: str, run_id: str, proposal_id: str) -> None:
    from app.services import portal_copilot_service

    tid = uuid.UUID(str(tenant_id))
    run(
        lambda: portal_copilot_service.on_draft_decided(tid, uuid.UUID(run_id), uuid.UUID(proposal_id)),
        tenant_id=tid,
    )


@celery_app.task(name="portal_copilot.purge_expired", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_expired() -> dict[str, Any]:
    from app.services import portal_copilot_service

    return run(lambda: for_each_tenant(portal_copilot_service.purge_expired))  # type: ignore[no-any-return]


__all__ = ["on_draft_decided", "on_run_finished", "purge_expired"]
