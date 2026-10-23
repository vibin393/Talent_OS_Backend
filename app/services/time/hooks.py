"""Registrations of the ``time`` domain (autodiscovered by ``core.hooks``; Epic 26 appends its own).

* Approval subject ``timesheet`` (callbacks synchronous in the deciding transaction, AD-24) and the named
route
  ``placement_time_route`` (Expenses / Leave reuse it through ``approvals.resolve_route``).
* Posting sources ``timesheet_entry``, ``adjustment_entry``, ``rate_adjustment`` (AD-23, pull-only).
* Event subscriptions: ``PlacementCreated`` / ``PlacementActivated`` -> config + period Timesheets;
  ``RateSegmentAdded`` -> retro rate adjustments.
* Notification types ``time.timesheet_rejected``, ``time.timesheet_missing``, ``time.timesheet_reminder``,
  ``time.pending_approvals_digest``; workflow snapshot loader ``timesheet``; SLA milestone
  ``(timesheet, step_decision)``; close check ``timesheets_pending``; offboarding verifier
  ``final_timesheet_approved``.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import subscribe
from app.services.approvals import register_approval_subject, register_route_resolver
from app.services.approvals.routes import _routes


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- approvals
def _register_approvals() -> None:
    from app.services.time import approval, configs

    register_approval_subject(
        approval.SUBJECT,
        on_final_decided=approval.on_final_decided,
        on_step_decided=approval.on_step_decided,
        describe=approval.describe,
        permission="timesheet:approve",
    )
    if approval.ROUTE not in _routes:
        register_route_resolver(approval.ROUTE, configs.placement_time_route)


_register_approvals()


# ---------------------------------------------------------------- posting sources
def _register_postings() -> None:
    from app.services.time import posting_source

    posting_source.register()


_register_postings()


# ---------------------------------------------------------------- events
def _on_placement(event: Any) -> None:
    from app.tasks import time as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.placement_event, str(event.tenant_id), str(event.placement_id))


def _on_rate_segment(event: Any) -> None:
    from app.tasks import time as tasks
    from app.tasks.runtime import enqueue

    if not event.superseded:
        return
    superseded = [[str(s[0]), str(s[1]), str(s[2]) if s[2] is not None else None] for s in event.superseded]
    enqueue(
        tasks.rate_segment_added,
        str(event.tenant_id),
        str(event.placement_id),
        str(event.rate_segment_id),
        superseded,
    )


def _subscribe() -> None:
    from app.events.placements import PlacementActivated, PlacementCreated, RateSegmentAdded

    subscribe(PlacementCreated, _on_placement)
    subscribe(PlacementActivated, _on_placement)
    subscribe(RateSegmentAdded, _on_rate_segment)


_subscribe()


# ---------------------------------------------------------------- notifications
async def _ts(db: AsyncSession, payload: Any) -> Any:
    from app.models.time import Timesheet

    tid = _uuid(payload.get("timesheet_id"))
    if tid is None:
        return None
    return (await db.execute(select(Timesheet).where(Timesheet.id == tid))).scalar_one_or_none()


async def _worker(db: AsyncSession, payload: Any) -> set[Any]:
    from app.services.notifications_service import Recipient

    ts = await _ts(db, payload)
    return {Recipient("candidate", ts.candidate_id)} if ts is not None else set()


async def _worker_and_am(db: AsyncSession, payload: Any) -> set[Any]:
    from app.models.placements import Placement
    from app.services.notifications_service import Recipient

    ts = await _ts(db, payload)
    if ts is None:
        return set()
    out: set[Any] = {Recipient("candidate", ts.candidate_id)}
    am = (
        await db.execute(select(Placement.account_manager_user_id).where(Placement.id == ts.placement_id))
    ).scalar_one_or_none()
    if am is not None:
        out.add(Recipient("user", am))
    return out


async def _reminder_target(db: AsyncSession, payload: Any) -> set[Any]:  # ruff: ignore[unused-function-argument]
    from app.services.notifications_service import Recipient

    rid = _uuid(payload.get("recipient_id"))
    kind = str(payload.get("recipient_kind") or "")
    return (
        {Recipient(kind, rid)} if rid is not None and kind in ("user", "candidate", "client_user") else set()
    )


async def _digest_user(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
    uid = _uuid(payload.get("user_id"))
    return {uid} if uid else set()


def _content(title: str, ts: Any, *, worker: bool, body: str | None = None) -> Any:
    from app.services.notifications_service import Content

    if ts is None:
        return Content(title=title[:300], body=body)
    path = f"/portal/work/timesheets/{ts.id}" if worker else f"/timesheets/{ts.id}"
    return Content(title=title[:300], body=body, entity_type="timesheet", entity_id=ts.id, link_path=path)


async def _render_rejected(db: AsyncSession, payload: Any) -> Any:
    ts = await _ts(db, payload)
    label = f"Timesheet {ts.human_id}" if ts is not None else "Your timesheet"
    comment = ts.rejection_comment if ts is not None else None
    return _content(f"{label} was rejected -- please correct and resubmit", ts, worker=True, body=comment)


async def _render_missing(db: AsyncSession, payload: Any) -> Any:
    ts = await _ts(db, payload)
    label = f"Timesheet {ts.human_id}" if ts is not None else "A timesheet"
    return _content(f"{label} for the period ending {payload.get('period_end')} is missing", ts, worker=True)


async def _render_reminder(db: AsyncSession, payload: Any) -> Any:
    ts = await _ts(db, payload)
    label = f"Timesheet {ts.human_id}" if ts is not None else "A timesheet"
    worker = payload.get("recipient_kind") == "candidate"
    text = f"Reminder: please submit {label}" if worker else f"Reminder: {label} is waiting for your approval"
    return _content(text, ts, worker=worker)


async def _render_digest(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
    from app.services.notifications_service import Content

    return Content(
        title=f"{payload.get('pending_count')} timesheet approval(s) waiting for you", link_path="/approvals"
    )


def _register_notifications() -> None:
    from app.events.time import TimesheetMissing, TimesheetRejected
    from app.services.notifications_service import (
        NotificationType,
        register_event_source,
        register_notification_type,
    )
    from app.services.time import events as ev

    types = {
        "time.timesheet_rejected": (
            NotificationType("Timesheet rejected", "Timesheets", "both", True, _worker, _render_rejected),
            TimesheetRejected,
        ),
        "time.timesheet_missing": (
            NotificationType(
                "Timesheet missing", "Timesheets", "both", False, _worker_and_am, _render_missing
            ),
            TimesheetMissing,
        ),
        "time.timesheet_reminder": (
            NotificationType(
                "Timesheet reminder", "Timesheets", "both", False, _reminder_target, _render_reminder
            ),
            ev.TimesheetReminderDue,
        ),
        "time.pending_approvals_digest": (
            NotificationType(
                "Pending timesheet approvals", "Timesheets", "in_app", False, _digest_user, _render_digest
            ),
            ev.TimesheetApprovalsDigestDue,
        ),
    }
    for key, (definition, event_cls) in types.items():
        try:
            register_notification_type(key, definition)
        except Exception:
            logger.debug("notification type {} already registered", key)
            continue
        register_event_source(event_cls, key)


_register_notifications()


# ---------------------------------------------------------------- workflow, SLA, close check, offboarding
async def _snapshot(ctx: RequestContext, db: AsyncSession, timesheet_id: uuid.UUID) -> dict[str, Any] | None:  # ruff: ignore[unused-function-argument]
    from app.models.time import Timesheet

    ts = (await db.execute(select(Timesheet).where(Timesheet.id == timesheet_id))).scalar_one_or_none()
    if ts is None:
        return None
    return {
        "id": str(ts.id),
        "status": ts.status,
        "kind": ts.kind,
        "placement_id": str(ts.placement_id),
        "client_id": str(ts.client_id),
        "legal_entity_id": str(ts.legal_entity_id),
        "period_end": ts.period_end.isoformat(),
    }


def _register_platform() -> None:
    from app.services.legal_entities.periods import close_checks, register_close_check
    from app.services.placements_service import offboarding_verifiers, register_offboarding_verifier
    from app.services.sla.registry import SlaMilestoneDef, milestone, register_sla_milestone
    from app.services.time import monitoring
    from app.services.workflow import catalogue
    from app.services.workflow.conditions import FieldDef

    if catalogue.snapshot_loader("timesheet") is None:
        catalogue.register_snapshot_loader(
            "timesheet",
            _snapshot,
            {
                "status": FieldDef(
                    "enum", "Status", ("draft", "submitted", "in_approval", "approved", "rejected")
                ),
                "kind": FieldDef("enum", "Kind", ("regular", "adjustment")),
                "placement_id": FieldDef("uuid", "Placement"),
                "client_id": FieldDef("uuid", "Client"),
                "legal_entity_id": FieldDef("uuid", "Legal entity"),
            },
        )
    if milestone("timesheet", "step_decision") is None:
        register_sla_milestone(
            "timesheet",
            "step_decision",
            SlaMilestoneDef("Timesheet approval step decision", scope_keys=("route_step",)),
        )
    if "timesheets_pending" not in close_checks():
        register_close_check("timesheets_pending", monitoring.close_check)
    if "final_timesheet_approved" not in offboarding_verifiers():
        register_offboarding_verifier("final_timesheet_approved", monitoring.final_timesheet_approved)


try:
    _register_platform()
except Exception:  # pragma: no cover - never block other domains' registrations
    logger.exception("time platform registrations failed")


# ---------------------------------------------------------------- Epic 26: Leave & Holidays
def _register_leave() -> None:
    from app.services.time import leave_hooks

    leave_hooks.register()


try:
    _register_leave()
except Exception:  # pragma: no cover - never block other domains' registrations
    logger.exception("leave registrations failed")
