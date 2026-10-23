# ruff: file-ignore[line-too-long]
"""Registrations of the ``workforce`` domain (autodiscovered by ``core.hooks``; Epic 44, spec A2 / A3 / A6).

* Approval subject ``workforce_plan`` (permission ``workforce_plan:approve``) and the human-only (★) transition
  ``workforce_plan -> approved`` (FR-257): AI / workflow / system / API-key approvers are refused by the engine and
  by ``approval.on_final_decided`` (``require_human``).
* Notification types (foundation 0.7 keys) ``workforce_plan_approval_requested`` (approvers) and
  ``workforce_plan_decided`` (owner); domain-local ``workforce_slot_filled`` (owner) and
  ``workforce_client_forecast_submitted`` (the Client's Account Manager, in-app + email).
* After-commit handlers (tasks on ``maintenance``): ``DemandOpened``, ``DemandStatusChanged``,
  ``PlacementActivated`` drive slot status. ``InternalTransferConfirmed`` (Epic 41) is informational only: a
  transfer fills a slot through ``PlacementActivated``.
* Audit action descriptions.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

AUDIT_ACTIONS = (
    ("workforce.plan_submitted", "A workforce plan (or revision) was submitted for approval"),
    ("workforce.plan_decided", "A workforce plan approval was decided"),
    ("workforce.plan_copied", "A workforce plan was copied from another plan"),
    ("workforce.plan_deleted", "A draft workforce plan was deleted"),
    ("workforce.revision_started", "A revision of an approved workforce plan was started"),
    ("workforce.revision_discarded", "A workforce plan revision was discarded"),
    ("workforce.lines_imported", "Workforce plan lines were imported from a file"),
    ("workforce.slots_converted", "Headcount slots were converted into draft Demands"),
    ("workforce.slot_cancelled", "A headcount slot was cancelled"),
    ("workforce.slot_claimed", "A manually created Demand claimed a headcount slot"),
    ("workforce.slot_released", "A cancelled / closed Demand released its headcount slot"),
    ("workforce.actuals_exported", "Plan vs actual was exported"),
    ("workforce.client_forecast_submitted", "A Client submitted a hiring forecast"),
    ("workforce.client_forecast_withdrawn", "A Client withdrew a hiring forecast"),
)


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _plan(db: AsyncSession, payload: Any) -> Any:
    from app.models.workforce import WorkforcePlan

    pid = _uuid(payload.get("workforce_plan_id"))
    if pid is None:
        return None
    return (await db.execute(select(WorkforcePlan).where(WorkforcePlan.id == pid))).scalar_one_or_none()


# ---------------------------------------------------------------- notifications
async def _approver_recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    from app.services import identity_bridge

    plan = await _plan(db, payload)
    if plan is None:
        return set()
    out = {u["id"] for u in await identity_bridge.users_with_permission(db, "workforce_plan:approve")}
    out.add(plan.owner_id)
    return out


async def _owner_recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    plan = await _plan(db, payload)
    return {plan.owner_id} if plan is not None else set()


def _renderer(template: str) -> Any:
    async def render(db: AsyncSession, payload: Any) -> Any:
        from app.services.notifications_service import Content

        plan = await _plan(db, payload)
        name = plan.name if plan is not None else "Workforce plan"
        return Content(
            title=template.format(
                name=name, outcome=str(payload.get("outcome") or "decided").replace("_", " ")
            )[:300],
            entity_type="workforce_plan",
            entity_id=plan.id if plan is not None else None,
            link_path=f"/workforce/{plan.id}" if plan is not None else None,
        )

    return render


def _register_notifications() -> None:
    from app.events.workforce import (
        ClientForecastSubmitted,
        WorkforcePlanDecided,
        WorkforcePlanSubmitted,
        WorkforceSlotFilled,
    )
    from app.services.notifications_service import (
        NotificationType,
        register_event_source,
        register_notification_type,
    )

    types = {
        "workforce_plan_approval_requested": NotificationType(
            "Workforce plan awaiting approval", "Workforce planning", "both", False,
            _approver_recipients, _renderer("Workforce plan awaiting approval: {name}"), internal_only=True,
        ),
        "workforce_plan_decided": NotificationType(
            "Workforce plan approved or rejected", "Workforce planning", "both", False,
            _owner_recipients, _renderer("Workforce plan {name}: {outcome}"), internal_only=True,
        ),
        "workforce_slot_filled": NotificationType(
            "Headcount slot filled", "Workforce planning", "in_app", False,
            _owner_recipients, _renderer("A headcount slot of {name} was filled"), internal_only=True,
        ),
        "workforce_client_forecast_submitted": NotificationType(
            "Client hiring forecast received", "Workforce planning", "both", False,
            _owner_recipients, _renderer("New client hiring forecast: {name}"), internal_only=True,
        ),
    }  # fmt: skip
    for key, definition in types.items():
        try:
            register_notification_type(key, definition)
        except Exception:
            logger.debug("notification type {} already registered", key)
            return
    register_event_source(WorkforcePlanSubmitted, "workforce_plan_approval_requested")
    register_event_source(WorkforcePlanDecided, "workforce_plan_decided")
    register_event_source(WorkforceSlotFilled, "workforce_slot_filled")
    register_event_source(ClientForecastSubmitted, "workforce_client_forecast_submitted")


# ---------------------------------------------------------------- approvals / human-only
def _register_core() -> None:
    from app.core import audit
    from app.services.approvals import register_approval_subject
    from app.services.workflow.catalogue import register_human_only_transitions
    from app.services.workforce import approval
    from app.services.workforce.common import APPROVAL_SUBJECT, APPROVE

    register_human_only_transitions(APPROVAL_SUBJECT, ["approved"])
    register_approval_subject(
        APPROVAL_SUBJECT,
        on_final_decided=approval.on_final_decided,
        describe=approval.describe,
        permission=APPROVE,
    )
    for key, desc in AUDIT_ACTIONS:
        audit.register_audit_action(key, desc)


# ---------------------------------------------------------------- after-commit handlers
def _subscribe() -> None:
    from app.core.events import subscribe
    from app.events.demands import DemandOpened, DemandStatusChanged
    from app.events.placements import PlacementActivated

    def on_opened(e: DemandOpened) -> None:
        from app.tasks import workforce as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.demand_opened, str(e.tenant_id), str(e.demand_id))

    def on_status(e: DemandStatusChanged) -> None:
        if e.to_status not in ("cancelled", "closed"):
            return
        from app.tasks import workforce as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.demand_status_changed, str(e.tenant_id), str(e.demand_id), e.to_status)

    def on_activated(e: PlacementActivated) -> None:
        from app.tasks import workforce as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.placement_activated, str(e.tenant_id), str(e.placement_id))

    subscribe(DemandOpened, on_opened)
    subscribe(DemandStatusChanged, on_status)
    subscribe(PlacementActivated, on_activated)


for _fn in (_register_core, _register_notifications, _subscribe):
    _fn()
