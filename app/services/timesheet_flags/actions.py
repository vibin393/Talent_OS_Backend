# ruff: noqa: E501
"""Timesheet Agent actions (Epic 63 A2 / A5): the reminder send (P2 ``remind`` only) and the approval escalation Task.

* ``send_reminders`` -- every id must be in the Account Manager's plan today (else ``skipped: not_in_plan``); calls
  ``time_service.remind(kind="scheduled")`` (its 4 h gap answers ``reminder_too_soon``). No other reminder path.
* ``escalate`` -- one Task per timesheet approval step for the AM, on the timesheet's Placement (the P1 task entity
  types have no ``timesheet``), titled "Timesheet {TS} waiting {n} days for {approver}"; a second call for the same
  step returns the first Task.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.models.placements import Placement
from app.models.tasks import Task
from app.models.time import Timesheet
from app.services.timesheet_flags import nudges


async def am_of(db: AsyncSession, timesheet_id: uuid.UUID) -> uuid.UUID | None:
    row = (
        await db.execute(
            select(Placement.account_manager_user_id)
            .join(Timesheet, Timesheet.placement_id == Placement.id)
            .where(Timesheet.id == timesheet_id)
        )
    ).first()
    return row[0] if row else None


async def send_reminders(
    ctx: RequestContext, db: AsyncSession, account_manager_id: uuid.UUID, timesheet_ids: list[uuid.UUID], today: date
) -> tuple[list[uuid.UUID], list[dict[str, str]]]:
    from app.services import time_service

    plan = await nudges.plan_for(db, account_manager_id, today)
    allowed = {i.timesheet_id for i in [*plan.submit, *plan.approve]}
    planned_any = {i.timesheet_id for i in [*plan.submit, *plan.approve, *plan.escalate, *plan.max_reached]}
    skipped: list[dict[str, str]] = []
    to_send: list[uuid.UUID] = []
    for tid in dict.fromkeys(timesheet_ids):
        if tid in allowed:
            to_send.append(tid)
        else:
            skipped.append({"id": str(tid), "reason": "not_in_plan" if tid not in planned_any else "not_due"})
    if not to_send:
        return [], skipped
    out = await time_service.remind(ctx, db, to_send, kind="scheduled")
    skipped += [{"id": str(s.timesheet_id), "reason": s.code} for s in out.skipped]
    return list(out.sent), skipped


async def escalate(
    ctx: RequestContext, db: AsyncSession, owner_id: uuid.UUID, timesheet_id: uuid.UUID, days_pending: int
) -> uuid.UUID:
    from app.schemas.tasks import TaskCreate
    from app.services import entity_refs, tasks_service

    facts, am_by_ts = await nudges.facts_for(db, timesheet_ids=[timesheet_id])
    if not facts:
        raise NotFound("Timesheet not found")
    f = facts[0]
    if f.status not in nudges.APPROVE_STATUSES:
        raise ValidationFailed("Only timesheets waiting for approval can be escalated", code="not_pending")
    marker = nudges.escalation_marker(f.timesheet_id, f.step_key)
    existing = (
        await db.execute(
            select(Task.id).where(Task.entity_type == "placement", Task.entity_id == f.placement_id, Task.notes.like(f"%{marker}%"))
        )
    ).first()
    if existing is not None:
        return uuid.UUID(str(existing[0]))
    am = am_by_ts.get(timesheet_id) or owner_id
    approver = "the approver"
    if f.recipient_kind == "user" and f.recipient_id is not None:
        info = (await entity_refs.users(db, [f.recipient_id])).get(f.recipient_id)
        if info is not None:
            approver = info.name
    elif f.recipient_kind == "client_user":
        approver = "the client approver"
    if f.approver_step:
        approver = f"{approver} ({f.approver_step})"
    days = max(days_pending, 0)
    data = TaskCreate.model_construct(
        title=f"Timesheet {f.human_id} waiting {days} days for {approver}"[:200],
        notes=f"Created by the Timesheet Agent: approval pending {days} business days. {marker}",
        owner_id=am, entity_type="placement", entity_id=f.placement_id,
        due_at=datetime.now(UTC) + timedelta(hours=24), due_all_day=False, priority="normal", reminder_at=None,
    )  # fmt: skip
    task = await tasks_service.create_system_task(ctx, db, data, source="agent")
    return task.id


async def max_reached_task(ctx: RequestContext, db: AsyncSession, am_id: uuid.UUID, item: Any) -> uuid.UUID | None:
    """After the last submit nudge: one Task for the AM per timesheet ("[ts-escalation:<ts>:submit]")."""
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    marker = nudges.escalation_marker(item.timesheet_id, "submit")
    existing = (
        await db.execute(select(Task.id).where(Task.entity_type == "placement", Task.notes.like(f"%{marker}%")))
    ).first()
    if existing is not None:
        return None
    data = TaskCreate.model_construct(
        title=f"Timesheet {item.human_id} still not submitted after {item.nudges_sent} reminders"[:200],
        notes=f"Created by the Timesheet Agent. {marker}", owner_id=am_id, entity_type="placement",
        entity_id=item.placement_id, due_at=datetime.now(UTC) + timedelta(hours=24), due_all_day=False,
        priority="normal", reminder_at=None,
    )  # fmt: skip
    task = await tasks_service.create_system_task(ctx, db, data, source="agent")
    return task.id


__all__ = ["am_of", "escalate", "max_reached_task", "send_reminders"]
