"""Registrations of Leave & Holidays (Epic 26), called from ``time/hooks.py`` (autodiscovered, AD-31).

* Approval subject ``leave_request`` (synchronous callbacks, AD-24; permission ``leave:approve``).
* Timesheet prefill sources ``holidays`` and ``approved_leave`` (A6).
* ``HolidayCalendarChanged`` -> re-prefill the draft Timesheets of the placements using the calendar.
* Notification types ``time.leave_decided`` (Worker), ``time.leave_client_notice`` (Client users, notify
  mode).
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _approvals() -> None:
    from app.services.approvals import register_approval_subject
    from app.services.time import leave_requests

    register_approval_subject(
        leave_requests.SUBJECT,
        on_final_decided=leave_requests.on_final_decided,
        on_step_decided=leave_requests.on_step_decided,
        describe=leave_requests.describe,
        permission="leave:approve",
    )


def _prefill() -> None:
    from app.services.time import holidays, leave_requests
    from app.services.time.prefill import HOLIDAY_SOURCE, LEAVE_SOURCE, register_prefill_source

    register_prefill_source(HOLIDAY_SOURCE, holidays.holiday_prefill)
    register_prefill_source(LEAVE_SOURCE, leave_requests.leave_prefill)


def _on_calendar_changed(event: Any) -> None:
    from app.tasks import time as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.leave_calendar_changed, str(event.tenant_id), str(event.calendar_id))


def _events() -> None:
    from app.core.events import subscribe
    from app.services.time.leave_events import HolidayCalendarChanged

    subscribe(HolidayCalendarChanged, _on_calendar_changed)


# ---------------------------------------------------------------- notifications
async def _request(db: AsyncSession, payload: Any) -> Any:
    from app.models.time import LeaveRequest

    rid = _uuid(payload.get("leave_request_id"))
    if rid is None:
        return None
    return (await db.execute(select(LeaveRequest).where(LeaveRequest.id == rid))).scalar_one_or_none()


async def _worker(db: AsyncSession, payload: Any) -> set[Any]:
    from app.services.notifications_service import Recipient

    req = await _request(db, payload)
    return {Recipient("candidate", req.candidate_id)} if req is not None else set()


async def _client_users(db: AsyncSession, payload: Any) -> set[Any]:
    from app.models.external_auth import ClientUser
    from app.services.notifications_service import Recipient

    cid = _uuid(payload.get("client_id"))
    if cid is None:
        return set()
    rows = (
        await db.execute(
            select(ClientUser.id).where(ClientUser.client_id == cid, ClientUser.status == "active")
        )
    ).scalars()
    return {Recipient("client_user", r) for r in rows}


_OUTCOME_TEXT = {
    "approved": "was approved",
    "rejected": "was rejected",
    "cancelled": "was cancelled",
    "cancellation_rejected": "cancellation was declined",
}


async def _render_decided(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    req = await _request(db, payload)
    label = f"Leave request {req.human_id}" if req is not None else "Your leave request"
    text = _OUTCOME_TEXT.get(str(payload.get("outcome")), "was decided")
    return Content(
        title=f"{label} {text}"[:300],
        body=req.decision_comment if req is not None else None,
        link_path="/portal/work/leave",
    )


async def _render_client_notice(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    req = await _request(db, payload)
    if req is None:
        return Content(title="A worker will be on leave")
    return Content(
        title=f"A worker will be on leave from {req.start_date.isoformat()} to {req.end_date.isoformat()}"[
            :300
        ],
        link_path="/client/leave-calendar",
    )


def _notifications() -> None:
    from app.events.time import LeaveRequestDecided
    from app.services.notifications_service import (
        NotificationType,
        register_event_source,
        register_notification_type,
    )
    from app.services.time.leave_events import LeaveClientNotice

    types = {
        "time.leave_decided": (
            NotificationType("Leave decided", "Leave", "both", True, _worker, _render_decided),
            LeaveRequestDecided,
        ),
        "time.leave_client_notice": (
            NotificationType(
                "Worker leave notice", "Leave", "both", False, _client_users, _render_client_notice
            ),
            LeaveClientNotice,
        ),
    }
    for key, (definition, event_cls) in types.items():
        try:
            register_notification_type(key, definition)
        except Exception:
            logger.debug("notification type {} already registered", key)
            continue
        register_event_source(event_cls, key)


def register() -> None:
    for step in (_approvals, _prefill, _events, _notifications):
        try:
            step()
        except Exception:  # pragma: no cover - never block other registrations
            logger.exception("leave registration {} failed", step.__name__)
