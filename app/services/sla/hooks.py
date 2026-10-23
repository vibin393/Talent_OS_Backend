# ruff: file-ignore[unused-function-argument, line-too-long]
"""Registrations of the ``sla`` domain (autodiscovered, Epic 32).

* Notification types ``sla_clock_at_risk``, ``sla_clock_breached`` and ``escalation_raised`` (group "SLA",
  channel ``both``, mandatory in-app); the evaluators deliver them directly (``notifications_service.deliver``).
* Workflow triggers ``sla.at_risk``, ``sla.breached`` (``SlaClockStatusChanged``) and ``sla.escalation_fired``
  (``EscalationFired``), with snapshot loaders for their entities (``sla_clock`` / ``escalation_watch``).
* Escalation watch lifecycle: ``ApprovalStepActivated`` opens the step's watch, ``ApprovalCompleted`` and
  ``TaskCompleted`` close watches (each through a task enqueued after the commit).

SLA milestones themselves are registered by the domains that own the timed subjects.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import subscribe
from app.events.approvals import ApprovalCompleted, ApprovalStepActivated
from app.events.tasks import TaskCompleted
from app.models.sla import EscalationWatch, SlaClock
from app.services import notifications_service as notifications
from app.services.workflow import registry as workflow_registry
from app.services.workflow.conditions import FieldDef

CLOCK_STATUSES = ("healthy", "at_risk", "breached", "met", "cancelled")


async def _payload_recipients(db: AsyncSession, payload: Mapping[str, Any]) -> set[uuid.UUID]:
    out: set[uuid.UUID] = set()
    for value in payload.get("recipient_ids") or ():
        try:
            out.add(uuid.UUID(str(value)))
        except ValueError:
            continue
    return out


def _render(title: str) -> Any:
    async def render(db: AsyncSession, payload: Mapping[str, Any]) -> notifications.Content:
        return notifications.Content(title=str(payload.get("title") or title), body=payload.get("body"))

    return render


def _register_notifications() -> None:
    for key, label in (
        ("sla_clock_at_risk", "SLA at risk"),
        ("sla_clock_breached", "SLA breached"),
        ("escalation_raised", "Escalation raised"),
    ):
        if key in notifications.NOTIFICATION_TYPES:
            continue
        notifications.register_notification_type(
            key,
            notifications.NotificationType(label, "SLA", "both", True, _payload_recipients, _render(label)),
        )


# ---------------------------------------------------------------- workflow triggers + snapshot loaders
async def _clock_snapshot(
    ctx: RequestContext, db: AsyncSession, clock_id: uuid.UUID
) -> dict[str, Any] | None:
    clock = (await db.execute(select(SlaClock).where(SlaClock.id == clock_id))).scalar_one_or_none()
    if clock is None:
        return None
    return {
        "id": str(clock.id),
        "subject_type": clock.subject_type,
        "subject_id": str(clock.subject_id),
        "milestone": clock.milestone,
        "status": clock.status,
        "vendor_id": str(clock.org_vendor_id) if clock.org_vendor_id else None,
        "client_id": str(clock.org_client_id) if clock.org_client_id else None,
    }


async def _watch_snapshot(
    ctx: RequestContext, db: AsyncSession, watch_id: uuid.UUID
) -> dict[str, Any] | None:
    watch = (
        await db.execute(select(EscalationWatch).where(EscalationWatch.id == watch_id))
    ).scalar_one_or_none()
    if watch is None:
        return None
    return {
        "id": str(watch.id),
        "subject_kind": watch.subject_kind,
        "subject_id": str(watch.subject_id),
        "fires": watch.fires,
        "status": watch.status,
        "action": (watch.spec or {}).get("action"),
    }


def _to_status(value: str) -> Any:
    def check(event: object) -> bool:
        return getattr(event, "to_status", None) == value

    return check


def _register_triggers() -> None:
    from app.services.workflow import catalogue

    catalogue.register_snapshot_loader(
        "sla_clock",
        _clock_snapshot,
        {
            "subject_type": FieldDef("str", "Subject type"),
            "milestone": FieldDef("str", "Milestone"),
            "status": FieldDef("enum", "SLA status", CLOCK_STATUSES),
            "vendor_id": FieldDef("uuid", "Vendor"),
            "client_id": FieldDef("uuid", "Client"),
        },
    )
    catalogue.register_snapshot_loader(
        "escalation_watch",
        _watch_snapshot,
        {
            "subject_kind": FieldDef("enum", "Escalated item", ("approval_step", "task")),
            "fires": FieldDef("int", "Escalation count"),
            "action": FieldDef("enum", "Escalation action", ("notify", "reassign")),
        },
    )
    extras = {
        "subject_type": ("subject_type", FieldDef("str", "Subject type")),
        "milestone": ("milestone", FieldDef("str", "Milestone")),
    }
    for key, label, status in (
        ("sla.at_risk", "SLA at risk", "at_risk"),
        ("sla.breached", "SLA breached", "breached"),
    ):
        if key in workflow_registry.TRIGGERS:
            continue
        workflow_registry.register_trigger(
            key,
            workflow_registry.Trigger(
                key,
                label,
                "app.events.sla",
                "SlaClockStatusChanged",
                "sla_clock",
                "clock_id",
                extras,
                _to_status(status),
            ),
        )
    if "sla.escalation_fired" not in workflow_registry.TRIGGERS:
        workflow_registry.register_trigger(
            "sla.escalation_fired",
            workflow_registry.Trigger(
                "sla.escalation_fired",
                "Escalation fired",
                "app.events.sla",
                "EscalationFired",
                "escalation_watch",
                "watch_id",
                {
                    "subject_kind": (
                        "subject_kind",
                        FieldDef("enum", "Escalated item", ("approval_step", "task")),
                    ),
                    "action": ("action", FieldDef("enum", "Escalation action", ("notify", "reassign"))),
                },
            ),
        )


# ---------------------------------------------------------------- watch lifecycle (after commit)
def _on_step_activated(event: ApprovalStepActivated) -> None:
    from app.tasks import sla as sla_tasks
    from app.tasks.runtime import enqueue

    enqueue(sla_tasks.open_approval_watch, str(event.tenant_id), str(event.request_id), int(event.position))


def _on_approval_completed(event: ApprovalCompleted) -> None:
    from app.tasks import sla as sla_tasks
    from app.tasks.runtime import enqueue

    enqueue(
        sla_tasks.close_watches,
        str(event.tenant_id),
        "approval_step",
        str(event.request_id),
        f"request_{event.status}",
    )


def _on_task_completed(event: TaskCompleted) -> None:
    from app.tasks import sla as sla_tasks
    from app.tasks.runtime import enqueue

    enqueue(sla_tasks.close_watches, str(event.tenant_id), "task", str(event.task_id), "task_completed")


_registered = False


def register() -> None:
    global _registered  # process-wide wiring, once
    if _registered:
        return
    _registered = True
    _register_notifications()
    _register_triggers()
    subscribe(ApprovalStepActivated, _on_step_activated)
    subscribe(ApprovalCompleted, _on_approval_completed)
    subscribe(TaskCompleted, _on_task_completed)


register()
