# ruff: noqa: E501
"""Developer Portal registrations (Epic 74, AD-31 autodiscovery): Epic 69's partner notifier (submission status emails +
org audit rows), the conformance fault resolver of ``core.middleware.sandbox_faults`` and the Celery kicks (sandbox
create / reset / delete on ``ecosystem``; run finalisation 2 minutes after "Finish run"). In eager mode (tests) the
sandbox work runs inline and finalisation is left to the caller."""

import uuid
from typing import Any

from app.core.middleware.sandbox_faults import register_fault_resolver
from app.services import marketplace_service
from app.services.developer_portal import notifier, sandboxes
from app.services.developer_portal.conformance import fault_injection, runner


def _eager() -> bool:
    from app.core.celery import celery_app

    return bool(celery_app.conf.task_always_eager)


def _kick_sandbox(op: str, sandbox_id: uuid.UUID, kw: dict[str, Any]) -> bool:
    if _eager():
        return False
    from app.tasks import developer_portal as tasks
    from app.tasks.runtime import enqueue

    task = {"create": tasks.create_sandbox, "reset": tasks.reset_sandbox, "delete": tasks.delete_sandbox}[op]
    enqueue(task, str(sandbox_id), **kw)
    return True


def _kick_finalize(run_id: uuid.UUID, countdown: int) -> bool:
    if _eager():
        return False
    from app.tasks import developer_portal as tasks

    tasks.finalize_conformance.apply_async(args=[str(run_id)], countdown=countdown)
    return True


marketplace_service.register_partner_notifier(notifier.on_submission_status)
register_fault_resolver(fault_injection.resolve)
sandboxes.set_kicker(_kick_sandbox)
runner.set_finalize_kicker(_kick_finalize)
