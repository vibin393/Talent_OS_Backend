# ruff: file-ignore[line-too-long]
"""Copilot Celery tasks (Epic 54). Every task starts with ``tenant_id`` (beat fan-outs excepted).

* queue ``conversations``: ``copilot.on_run_finished`` (store the answer, publish ``turn.completed|failed``, then the
  title on the first answer), ``copilot.on_draft_created`` (``draft.created``), ``copilot.on_draft_decided`` (event
  message), ``copilot.generate_title``;
* queue ``maintenance`` (daily 02:50 UTC): ``copilot.purge_expired`` -- conversations past the tenant retention.
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.tasks.runtime import enqueue, for_each_tenant, run

QUEUE = "conversations"
MAINTENANCE = "maintenance"


@celery_app.task(name="copilot.on_run_finished", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def on_run_finished(tenant_id: str, run_id: str) -> bool:
    from app.services import copilot_service

    tid, rid = uuid.UUID(str(tenant_id)), uuid.UUID(str(run_id))
    out = run(lambda: copilot_service.record_answer(tid, rid), tenant_id=tid)
    if out and out.get("first_answer") and not out.get("title_locked"):
        enqueue(generate_title, str(tid), str(out["conversation_id"]))
    return out is not None


@celery_app.task(name="copilot.on_draft_created", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def on_draft_created(tenant_id: str, run_id: str, proposal_id: str) -> None:
    from app.services import copilot_service

    tid = uuid.UUID(str(tenant_id))
    run(
        lambda: copilot_service.on_draft_created(tid, uuid.UUID(run_id), uuid.UUID(proposal_id)),
        tenant_id=tid,
    )


@celery_app.task(name="copilot.on_draft_decided", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def on_draft_decided(tenant_id: str, run_id: str, proposal_id: str, outcome: str | None = None) -> None:  # ruff: ignore[unused-function-argument]
    from app.services import copilot_service

    tid = uuid.UUID(str(tenant_id))
    run(
        lambda: copilot_service.on_draft_decided(tid, uuid.UUID(run_id), uuid.UUID(proposal_id)),
        tenant_id=tid,
    )


@celery_app.task(name="copilot.generate_title", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def generate_title(tenant_id: str, conversation_id: str, segment: bool = False) -> bool:
    from app.services import copilot_service

    tid = uuid.UUID(str(tenant_id))
    return bool(
        run(
            lambda: copilot_service.generate_title(tid, uuid.UUID(conversation_id), segment=segment),
            tenant_id=tid,
        )
    )


@celery_app.task(name="copilot.purge_expired", queue=MAINTENANCE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_expired() -> dict[str, Any]:
    from app.services import copilot_service

    return run(lambda: for_each_tenant(copilot_service.purge_expired))  # type: ignore[no-any-return]


__all__ = ["generate_title", "on_draft_created", "on_draft_decided", "on_run_finished", "purge_expired"]
