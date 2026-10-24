# ruff: file-ignore[line-too-long]
"""Registrations of the ``mobility`` domain (autodiscovered by ``core.hooks``), Epic 41.

* Approval subject ``internal_application`` (explicit steps; no subject permission -- the resolved manager decides)
  and the human-only (*) transition ``internal_application: transfer_confirmed`` (FR-257).
* P1 pipeline transition guard (holds an internal Application at ``applied`` until manager approval).
* P1 matching section ``internal`` (order 20; stored scores only).
* After-commit handlers on ``DemandOpened`` / ``DemandStatusChanged`` / ``DemandPublished`` / ``DemandUnpublished`` /
  ``DemandRequirementsChanged`` -> ``mobility.sync_opening`` (queue ``maintenance``).
* Notification types ``internal_application_submitted``, ``internal_application_decided`` (foundation keys) and the
  domain-local ``internal_transfer_confirmed``. Manager step notifications come from the approval engine.
* Settings entry and audit action descriptions.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

AUDIT_ACTIONS = (
    ("mobility.application_submitted", "An employee applied for an internal opening"),
    ("mobility.application_approved", "An internal application was approved by the employee's manager"),
    ("mobility.application_rejected", "An internal application was not approved by the employee's manager"),
    ("mobility.application_withdrawn", "An employee withdrew an internal application"),
    ("mobility.transfer_confirmed", "An internal transfer was confirmed"),
)


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- approvals / human-only / pipeline / matching
def _register_core() -> None:
    from app.services.approvals import register_approval_subject
    from app.services.matching.sections import register_section
    from app.services.mobility import approval, matches
    from app.services.mobility.applications import pipeline_guard
    from app.services.mobility.common import APPROVAL_SUBJECT, HUMAN_ONLY_TARGET
    from app.services.pipeline.applications import register_transition_guard
    from app.services.workflow.catalogue import register_human_only_transitions

    register_human_only_transitions(APPROVAL_SUBJECT, [HUMAN_ONLY_TARGET])
    register_approval_subject(
        APPROVAL_SUBJECT,
        on_final_decided=approval.on_final_decided,
        on_step_decided=approval.on_step_decided,
        describe=approval.describe,
        permission=None,
        auto_approve_calls_back=True,
    )
    register_transition_guard(pipeline_guard)
    register_section("internal", order=20, label="Internal", candidate_ids=matches.internal_candidate_ids)


# ---------------------------------------------------------------- after-commit handlers
def _subscribe() -> None:
    from app.core.events import subscribe
    from app.events.demands import (
        DemandOpened,
        DemandPublished,
        DemandRequirementsChanged,
        DemandStatusChanged,
        DemandUnpublished,
    )

    def on_demand(e: Any) -> None:
        from app.tasks import mobility as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.sync_opening, str(e.tenant_id), str(e.demand_id))

    for cls in (DemandOpened, DemandStatusChanged, DemandPublished, DemandUnpublished, DemandRequirementsChanged):

        def handler(e: Any) -> None:
            on_demand(e)

        handler.__name__ = f"mobility_sync_on_{cls.__name__}"
        subscribe(cls, handler)


# ---------------------------------------------------------------- notifications
async def _row(db: AsyncSession, payload: Any) -> Any:
    from app.models.mobility import MobilityApplication

    rid = _uuid(payload.get("mobility_application_id"))
    return await db.get(MobilityApplication, rid) if rid else None


def _employee(payload: Any) -> set[Any]:
    from app.services.notifications_service import Recipient

    if payload.get("employee_kind") == "user":
        uid = _uuid(payload.get("user_id"))
        return {uid} if uid else set()
    cid = _uuid(payload.get("candidate_id"))
    return {Recipient("candidate", cid)} if cid else set()


async def _submitted_recipients(db: AsyncSession, payload: Any) -> set[Any]:
    from app.models.demands import Demand

    out = _employee(payload)
    did = _uuid(payload.get("demand_id"))
    d = await db.get(Demand, did) if did else None
    if d is not None:
        out.add(d.owner_id)
    return out


async def _employee_recipients(db: AsyncSession, payload: Any) -> set[Any]:  # ruff: ignore[unused-function-argument]  # NOSONAR
    return _employee(payload)


async def _transfer_recipients(db: AsyncSession, payload: Any) -> set[Any]:
    from app.models.placements import Placement

    out = _employee(payload)
    for key in ("from_placement_id", "new_placement_id"):
        pid = _uuid(payload.get(key))
        p = await db.get(Placement, pid) if pid else None
        if p is not None and p.account_manager_user_id:
            out.add(p.account_manager_user_id)
    return out


async def _title(db: AsyncSession, payload: Any) -> str:
    from app.models.demands import Demand

    did = _uuid(payload.get("demand_id"))
    d = await db.get(Demand, did) if did else None
    return d.title if d else "an internal opening"


async def _render_submitted(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    return Content(
        title=f"Internal application submitted: {await _title(db, payload)}"[:300],
        body="The application goes to the employee's manager for approval before interviews.",
        link_path="/internal-jobs/my-applications",
    )


async def _render_decided(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    approved = payload.get("outcome") == "approved"
    row = await _row(db, payload)
    title = await _title(db, payload)
    body = (
        "Your manager approved the move. The recruiting team will contact you about next steps."
        if approved
        else f"Reason: {row.rejection_reason}" if row is not None and row.rejection_reason else None
    )
    return Content(
        title=(f"Your internal application for {title} was approved" if approved else f"Your internal application for {title} was not approved")[:300],
        body=body,
        link_path="/internal-jobs/my-applications",
    )


async def _render_transfer(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    return Content(title=f"Internal transfer confirmed: {await _title(db, payload)}"[:300], link_path="/internal-jobs/my-applications")


def _register_notifications() -> None:
    from app.events.mobility import InternalTransferConfirmed
    from app.services import notifications_service as ns
    from app.services.mobility.events import InternalApplicationDecided, InternalApplicationSubmitted

    portal = "/portal/work/opportunities/applications"
    defs: dict[str, tuple[Any, type]] = {
        "internal_application_submitted": (
            ns.NotificationType(
                "Internal application submitted", "Internal mobility", "in_app", False, _submitted_recipients, _render_submitted, portal_path=portal, external=True
            ),
            InternalApplicationSubmitted,
        ),
        "internal_application_decided": (
            ns.NotificationType(
                "Internal application decided", "Internal mobility", "both", False, _employee_recipients, _render_decided, portal_path=portal, external=True
            ),
            InternalApplicationDecided,
        ),
        "internal_transfer_confirmed": (
            ns.NotificationType(
                "Internal transfer confirmed", "Internal mobility", "both", False, _transfer_recipients, _render_transfer, portal_path=portal, external=True
            ),
            InternalTransferConfirmed,
        ),
    }
    for key, (definition, cls) in defs.items():
        if key in ns.NOTIFICATION_TYPES:
            continue
        ns.register_notification_type(key, definition)
        ns.register_event_source(cls, key)


# ---------------------------------------------------------------- settings / audit
def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "recruiting",
        "mobility",
        "Internal mobility",
        "Internal job board, worker board, manager approval steps, default tenure and the disclosure text.",
        "/api/v1/mobility/settings",
        "mobility:manage",
        owner_epic="Epic 41",
    )


def _register_audit() -> None:
    from app.core import audit

    for key, desc in AUDIT_ACTIONS:
        audit.register_audit_action(key, desc)


for _fn in (_register_core, _subscribe, _register_notifications, _register_settings, _register_audit):
    try:
        _fn()
    except Exception:  # pragma: no cover - never block start-up on an optional registry
        logger.exception("mobility: {} failed", _fn.__name__)
