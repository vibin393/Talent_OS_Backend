# ruff: file-ignore[line-too-long]
"""Registrations of the ``timesheet_flags`` domain (Epic 63), autodiscovered by ``core.hooks``.

* events (after commit; each enqueues a task):
  - P2 ``TimesheetSubmitted`` -> ``timesheet_flags.evaluate`` (queue ``interactive``; deterministic, AI on or off);
  - P2 ``TimesheetRejected`` -> ``timesheet_flags.supersede`` (the resubmission is evaluated anew; P2 has no separate
    ``TimesheetReopened`` event -- a reopen bumps ``lock_version`` and the next submission supersedes);
  - ``TimesheetFlagsRaised`` -> the Timesheet Agent ``flags_raised`` run (explanations; ``start_run`` refuses when the
    agent is off) and the notification ``timesheet_anomaly_flagged``.
* notification type ``timesheet_anomaly_flagged`` (internal only: the current internal approver and the Placement AM,
  never Client Users), audit labels, workflow trigger ``timesheet_flags.flags_raised``.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe


def _enqueue(task: Any, *args: Any) -> None:
    from app.tasks.runtime import enqueue

    enqueue(task, *args)


def _subscribe() -> None:
    from app.events.time import TimesheetRejected, TimesheetSubmitted
    from app.events.timesheet_flags import TimesheetFlagsRaised

    def on_submitted(e: TimesheetSubmitted) -> None:
        from app.tasks import timesheet_flags as tasks

        _enqueue(tasks.evaluate, str(e.tenant_id), str(e.timesheet_id))

    def on_rejected(e: TimesheetRejected) -> None:
        from app.tasks import timesheet_flags as tasks

        _enqueue(tasks.supersede, str(e.tenant_id), str(e.timesheet_id))

    def on_flags(e: TimesheetFlagsRaised) -> None:
        from app.tasks import notifications as notification_tasks
        from app.tasks import timesheet_flags as tasks

        _enqueue(tasks.start_flag_run, str(e.tenant_id), str(e.timesheet_id), int(e.timesheet_version))
        _enqueue(notification_tasks.fan_out, str(e.tenant_id), "timesheet_anomaly_flagged", {
            "event_id": f"timesheet_flags:{e.timesheet_id}:{e.timesheet_version}", "timesheet_id": str(e.timesheet_id),
            "placement_id": str(e.placement_id), "flag_count": e.flag_count, "max_severity": e.max_severity,
        })  # fmt: skip

    for fn, name in ((on_submitted, "submitted"), (on_rejected, "rejected"), (on_flags, "flags_raised")):
        fn.__name__ = f"timesheet_flags_on_{name}"
    subscribe(TimesheetSubmitted, on_submitted)
    subscribe(TimesheetRejected, on_rejected)
    subscribe(TimesheetFlagsRaised, on_flags)


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None


def _register_notifications() -> None:
    from app.services import notifications_service as ns

    async def recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
        from app.models.approvals import ApprovalRequest
        from app.models.placements import Placement
        from app.models.time import Timesheet

        out: set[uuid.UUID] = set()
        ts = await db.get(Timesheet, _uuid(payload.get("timesheet_id"))) if payload.get("timesheet_id") else None
        if ts is None:
            return out
        placement = await db.get(Placement, ts.placement_id)
        if placement is not None and placement.account_manager_user_id is not None:
            out.add(placement.account_manager_user_id)
        if ts.approval_request_id is not None:
            req = await db.get(ApprovalRequest, ts.approval_request_id)
            if req is not None and req.current_approver_user_id is not None:  # internal approvers only
                out.add(req.current_approver_user_id)
        return out

    async def render(db: AsyncSession, payload: Any) -> Any:
        from app.models.time import Timesheet

        tid = _uuid(payload.get("timesheet_id"))
        ts = await db.get(Timesheet, tid) if tid else None
        n = int(payload.get("flag_count") or 0)
        return ns.Content(
            title=f"Timesheet {ts.human_id if ts else ''} has {n} things to check".replace("  ", " "),
            entity_type="timesheet", entity_id=tid, link_path=f"/timesheets/{tid}" if tid else None,
        )  # fmt: skip

    if "timesheet_anomaly_flagged" not in ns.NOTIFICATION_TYPES:
        ns.register_notification_type(
            "timesheet_anomaly_flagged",
            ns.NotificationType(
                "Timesheet has things to check", "Timesheets", "in_app", False, recipients, render, internal_only=True
            ),
        )


def _register_audit() -> None:
    from app.core import audit

    for key, text in (
        ("timesheet_flag.reviewed", "A timesheet flag was marked reviewed"),
        ("timesheet_flag.dismissed", "A timesheet flag was dismissed"),
    ):
        audit.register_audit_action(key, text)


def _register_workflow_trigger() -> None:
    try:
        from app.services.workflow.registry import TRIGGERS, FieldDef, Trigger, register_trigger
    except ImportError:  # pragma: no cover - workflow absent
        return
    key = "timesheet_flags.flags_raised"
    if key in TRIGGERS:
        return
    register_trigger(
        key,
        Trigger(
            key, "Timesheet anomaly flags raised", "app.events.timesheet_flags", "TimesheetFlagsRaised", "timesheet",
            "timesheet_id",
            {
                "flag_count": ("flag_count", FieldDef("int", "Flags")),
                "max_severity": ("max_severity", FieldDef("enum", "Highest severity", ("info", "warning"))),
            },
        ),
    )  # fmt: skip


for _fn in (_subscribe, _register_notifications, _register_audit, _register_workflow_trigger):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("timesheet_flags hooks: {} failed", _fn.__name__)
        raise
