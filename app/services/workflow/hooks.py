# ruff: file-ignore[line-too-long]
"""Registrations of the ``workflow`` domain (autodiscovered, Epic 32).

* The Phase 2 trigger catalogue (``catalogue.register_catalogue``; ``subscribe_optional`` on the contract-layer
  events, so no Phase 2 domain is imported).
* Approval subject ``workflow_step`` (approval nodes) and the ``WorkflowWaitResumed`` handler that continues the
  run after the decision commits.
* Notification types ``workflow_approval_requested`` (the approver is notified through the P1
  ``approval_requested`` fan-out of ``ApprovalStepActivated``; this key labels it in preferences) and
  ``workflow_run_failed`` (the rule owner, on a failed graph run).
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe
from app.events.workflow import WorkflowRunCompleted, WorkflowWaitResumed
from app.models.workflow import WorkflowRule
from app.services import notifications_service as notifications
from app.services.workflow import catalogue, waits


async def _rule_owner(db: AsyncSession, payload: Mapping[str, Any]) -> set[uuid.UUID]:
    try:
        rule_id = uuid.UUID(str(payload.get("rule_id")))
    except ValueError:
        return set()
    owner = (
        await db.execute(select(WorkflowRule.created_by).where(WorkflowRule.id == rule_id))
    ).scalar_one_or_none()
    return {owner} if owner else set()


async def _render_failed(db: AsyncSession, payload: Mapping[str, Any]) -> notifications.Content:  # ruff: ignore[unused-function-argument]  # NOSONAR
    return notifications.Content(
        title=f"A workflow run ended {str(payload.get('status', 'failed')).replace('_', ' ')}",
        entity_type="workflow_execution",
        entity_id=uuid.UUID(str(payload["execution_id"])) if payload.get("execution_id") else None,
        link_path=f"/admin/workflows/{payload.get('rule_id')}" if payload.get("rule_id") else None,
    )


def _failed_type(payload: Mapping[str, Any]) -> str | None:
    return "workflow_run_failed" if payload.get("status") in ("failed", "partially_failed") else None


def _on_wait_resumed(event: WorkflowWaitResumed) -> None:
    from app.tasks import workflow as workflow_tasks
    from app.tasks.runtime import enqueue

    enqueue(workflow_tasks.continue_run, str(event.tenant_id), str(event.wait_id))


_registered = False


def register() -> None:
    global _registered  # process-wide wiring, once
    if _registered:
        return
    _registered = True
    catalogue.register_catalogue()
    waits.register_subject()
    approval_requested = notifications.NOTIFICATION_TYPES["approval_requested"]
    if "workflow_approval_requested" not in notifications.NOTIFICATION_TYPES:
        notifications.register_notification_type(
            "workflow_approval_requested",
            notifications.NotificationType(
                "Workflow approval requested",
                "Workflow",
                "both",
                True,
                approval_requested.recipients_resolver,
                approval_requested.render,
            ),
        )
    if "workflow_run_failed" not in notifications.NOTIFICATION_TYPES:
        notifications.register_notification_type(
            "workflow_run_failed",
            notifications.NotificationType(
                "Workflow run failed", "Workflow", "in_app", False, _rule_owner, _render_failed
            ),
        )
        notifications.register_event_source(WorkflowRunCompleted, _failed_type)
    subscribe(WorkflowWaitResumed, _on_wait_resumed)


register()
