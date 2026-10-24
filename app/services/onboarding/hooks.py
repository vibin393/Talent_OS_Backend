# ruff: file-ignore[line-too-long, unused-function-argument]
"""Registrations of the ``onboarding`` domain (Epic 23 A8; autodiscovered by ``core.hooks``).

* Event subscriptions -> ``onboarding.handle_event`` (queue ``interactive``): ``PlacementCreated`` /
  ``PlacementDropped``, ``ComplianceRequirementsEvaluated`` / ``CredentialRecorded`` / ``CredentialVerified``
  (compliance is a sibling: events only), ``SignatureRequestCompleted`` / ``SignatureRequestStatusChanged``,
  ``TaskCompleted`` (internal tasks) and ``BankDetailsChangeDecided`` (replaces the approval-subject listener).
* Start-gate kind ``onboarding`` (resolver), Worker 360 section ``onboarding``, SLA milestone
  ``('onboarding_case', 'completion')``.
* Workflow triggers ``onboarding.case_created|task_submitted|task_returned|case_completed`` (+ the catalogue's
  ``onboarding.task_overdue`` snapshot loader), action ``onboarding.create_case``; ★ waive is human-only.
* Notification types ``onboarding_invited``, ``onboarding_task_returned``, ``onboarding_task_overdue``,
  ``onboarding.requirements_added``, ``onboarding.no_template``.
* Retention bindings ``onboarding_tasks`` / ``onboarding_acknowledgements`` (class ``worker_profile``).
"""

import importlib
import uuid
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe

_EVENTS = {
    "app.events.placements": ("PlacementCreated", "PlacementDropped", "BankDetailsChangeDecided"),
    "app.events.compliance": ("ComplianceRequirementsEvaluated", "CredentialRecorded", "CredentialVerified"),
    "app.events.documents": ("SignatureRequestCompleted", "SignatureRequestStatusChanged"),
    "app.events.tasks": ("TaskCompleted",),
}


def _handler(name: str) -> Any:
    def handle(event: Any) -> None:
        if name in ("SignatureRequestCompleted", "SignatureRequestStatusChanged"):
            if getattr(event, "subject_kind", None) != "onboarding_task":
                return
        from app.services.notifications_service import event_payload
        from app.tasks import onboarding as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.handle_event, str(event.tenant_id), name, event_payload(event))

    handle.__name__ = f"onboarding_on_{name}"
    return handle


def _subscribe() -> None:
    for module_name, names in _EVENTS.items():
        try:
            module = importlib.import_module(module_name)
        except ImportError:  # pragma: no cover
            continue
        for name in names:
            cls = getattr(module, name, None)
            if cls is not None:
                subscribe(cls, _handler(name))


_subscribe()


# ---------------------------------------------------------------- start gate, Worker 360, SLA
def _register_placements() -> None:
    from app.services.onboarding import cases
    from app.services.placements_service import (
        StartGateKindDef,
        register_start_gate_kind,
        register_worker_360_section,
        start_gate_kinds,
    )

    if "onboarding" not in start_gate_kinds():
        register_start_gate_kind(
            "onboarding", StartGateKindDef("Onboarding", overridable=True, resolver=cases.gate_resolver)
        )
    try:
        register_worker_360_section("onboarding", cases.worker_360)
    except Exception:
        logger.debug("worker 360 section onboarding already registered")


_register_placements()


def _register_sla() -> None:
    from app.services.onboarding import cases
    from app.services.sla.registry import SlaMilestoneDef, milestone, register_sla_milestone

    if milestone("onboarding_case", "completion") is None:
        register_sla_milestone(
            "onboarding_case",
            "completion",
            SlaMilestoneDef(
                "Onboarding completion before start",
                calendar="deadline",
                deadline=cases.sla_deadline,
                link=lambda case_id: f"/onboarding/{case_id}",
            ),
        )


_register_sla()


# ---------------------------------------------------------------- workflow
async def _snapshot(ctx: Any, db: AsyncSession, case_id: uuid.UUID) -> dict[str, Any] | None:
    from app.models.onboarding import OnboardingCase

    case = await db.get(OnboardingCase, case_id)
    if case is None:
        return None
    return {
        "id": str(case.id),
        "status": case.status,
        "placement_id": str(case.placement_id),
        "candidate_id": str(case.candidate_id),
        "progress_pct": case.progress_pct,
        "owner_user_id": str(case.owner_user_id) if case.owner_user_id else None,
    }


async def _create_case_run(actx: Any) -> str:
    from app.services.onboarding import cases

    if actx.entity_type != "placement":
        return "skipped: not a placement"
    case = await cases.handle_placement_created(actx.ctx, actx.db, actx.entity_id)
    return f"case {case.human_id}" if case is not None else "no matching template"


async def _create_case_dry(actx: Any) -> str:  # NOSONAR
    return f"would start onboarding for placement {actx.entity_id}"


def _register_workflow() -> None:
    from pydantic import BaseModel, ConfigDict

    from app.services.workflow import catalogue
    from app.services.workflow.actions import ACTIONS, ActionDef, register_action
    from app.services.workflow.conditions import FieldDef
    from app.services.workflow.registry import TRIGGERS, Trigger, register_trigger

    if catalogue.snapshot_loader("onboarding_case") is None:
        catalogue.register_snapshot_loader(
            "onboarding_case",
            _snapshot,
            {
                "status": FieldDef("enum", "Status", ("not_started", "in_progress", "complete", "cancelled")),
                "placement_id": FieldDef("uuid", "Placement"),
                "candidate_id": FieldDef("uuid", "Worker"),
                "progress_pct": FieldDef("int", "Progress %"),
                "owner_user_id": FieldDef("uuid", "Owner"),
            },
        )
    catalogue.register_human_only_transitions("onboarding_task", ["waived"])
    core = "app.events.onboarding"
    for trig in (
        Trigger(
            "onboarding.case_created",
            "Onboarding case created",
            core,
            "OnboardingCaseCreated",
            "onboarding_case",
            "case_id",
        ),
        Trigger(
            "onboarding.task_returned",
            "Onboarding task returned",
            core,
            "OnboardingTaskReturned",
            "onboarding_case",
            "case_id",
        ),
        Trigger(
            "onboarding.case_completed",
            "Onboarding case completed",
            core,
            "OnboardingCaseCompleted",
            "onboarding_case",
            "case_id",
        ),
        Trigger(
            "onboarding.task_submitted",
            "Onboarding progress changed",
            core,
            "OnboardingProgressChanged",
            "onboarding_case",
            "case_id",
        ),
    ):
        if trig.key not in TRIGGERS:
            try:
                register_trigger(trig.key, trig)
            except Exception:
                logger.debug("trigger {} already registered", trig.key)

    class CreateCaseParams(BaseModel):
        model_config = ConfigDict(extra="forbid")

    def _validate(params: Any, entity_type: str, path: str) -> None:
        if entity_type != "placement":
            from app.core.errors import ValidationFailed

            raise ValidationFailed(
                f"Invalid action at {path}: only for placement triggers",
                code="invalid_action",
                details={"path": path, "reason": "only for placement triggers"},
            )

    if "onboarding.create_case" not in ACTIONS:
        register_action(
            "onboarding.create_case",
            ActionDef(
                "Start onboarding",
                CreateCaseParams,
                _validate,
                _create_case_run,
                _create_case_dry,
                entity_types=("placement",),
            ),
        )


try:
    _register_workflow()
except Exception:  # pragma: no cover - registry shape drift must not break the app
    logger.exception("onboarding workflow registrations failed")


# ---------------------------------------------------------------- notifications
async def _payload_users(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # NOSONAR
    from app.services.onboarding.common import as_uuid

    return {u for u in (as_uuid(x) for x in payload.get("recipient_ids") or []) if u}


async def _payload_worker(db: AsyncSession, payload: Any) -> set[Any]:  # NOSONAR
    from app.services.notifications_service import Recipient
    from app.services.onboarding.common import as_uuid

    cid = as_uuid(payload.get("candidate_id"))
    return {Recipient("candidate", cid)} if cid else set()


async def _render(db: AsyncSession, payload: Any) -> Any:  # NOSONAR
    from app.services.notifications_service import Content

    return Content(
        title=str(payload.get("title") or "Onboarding update")[:300], link_path="/portal/work/onboarding"
    )


def _register_notifications() -> None:
    from app.services.notifications_service import (
        NOTIFICATION_TYPES,
        NotificationType,
        register_notification_type,
    )
    from app.services.onboarding import notify

    types = {
        notify.INVITED: NotificationType(
            "Onboarding invitation", "Onboarding", "both", True, _payload_worker, _render, "both"
        ),
        notify.TASK_RETURNED: NotificationType(
            "Onboarding task returned", "Onboarding", "both", True, _payload_worker, _render, "both"
        ),
        notify.TASK_OVERDUE: NotificationType(
            "Onboarding tasks overdue", "Onboarding", "both", False, _payload_worker, _render, "both"
        ),
        notify.REQUIREMENTS_ADDED: NotificationType(
            "Requirements added to a completed onboarding",
            "Onboarding",
            "both",
            False,
            _payload_users,
            _render,
        ),
        notify.NO_TEMPLATE: NotificationType(
            "No onboarding template matched", "Onboarding", "both", False, _payload_users, _render
        ),
    }
    for key, definition in types.items():
        if key in NOTIFICATION_TYPES:
            continue
        try:
            register_notification_type(key, definition)
        except Exception:
            logger.debug("notification type {} already registered", key)


_register_notifications()


# ---------------------------------------------------------------- retention (Epic 35 binds; inert until then)
async def _subject(db: AsyncSession, row_id: uuid.UUID, *, table_name: str) -> uuid.UUID | None:
    from app.models.onboarding import OnboardingAcknowledgement, OnboardingTask

    model = OnboardingTask if table_name == "onboarding_tasks" else OnboardingAcknowledgement
    return (await db.execute(select(model.candidate_id).where(model.id == row_id))).scalar_one_or_none()


async def _anchor(db: AsyncSession, candidate_id: uuid.UUID, *_: Any, **__: Any) -> Any:
    from app.models.placements import Placement

    return (
        await db.execute(
            select(func.max(func.coalesce(Placement.end_date, Placement.planned_end_date))).where(
                Placement.candidate_id == candidate_id
            )
        )
    ).scalar_one_or_none()


async def _expire(*_: Any, **__: Any) -> None:
    """Retention expiry is executed by the erasure handler (``onboarding_erasure``)."""
    return


def _register_retention() -> None:
    try:
        from app.services.privacy.retention_classes import register_retention_binding
    except ImportError:  # pragma: no cover
        return
    for table_name, pii in (
        ("onboarding_tasks", ("draft", "data", "return_comment")),
        ("onboarding_acknowledgements", ("ip", "user_agent")),
    ):
        try:

            async def subject(db: AsyncSession, row_id: uuid.UUID, _t: str = table_name) -> uuid.UUID | None:
                return await _subject(db, row_id, table_name=_t)

            register_retention_binding(
                table_name,
                "worker_profile",
                subject_resolver=subject,
                anchor_resolver=_anchor,
                expire_fn=_expire,
                pii_columns=pii,
            )
        except Exception:
            logger.debug("retention binding {} already registered", table_name)


_register_retention()
