# ruff: file-ignore[line-too-long]
"""Timesheet nudge plan (Epic 63 A2, FR-311) -- a pure planner plus its loader; no table of its own.

P2 stays the engine: P2 reminds Workers on its ``reminder_offsets`` (days 0 and 1 after the period end), flags missing
timesheets and sends approvers a daily digest. The plan only adds what P2 does not, for one Account Manager:

* **submit** -- ``draft`` / ``rejected`` regular timesheets whose period ended >= 2 business days ago, with no
  reminder (any kind) in the last 20 h; every ``SUBMIT_EVERY_DAYS`` business days, at most ``SUBMIT_MAX`` agent nudges;
  after the last one the item is listed in ``max_reached`` (the AM gets a Task).
* **approve** -- ``submitted`` / ``in_approval`` timesheets pending >= 2 business days at the current step, no
  reminder in the last 20 h; every 2 business days.
* **escalate** -- pending >= ``ESCALATE_AFTER_DAYS`` business days at the current step and no escalation Task yet for
  that step (``escalation_marker``).

Agent nudges are P2 ``remind(kind="scheduled")`` rows with ``sent_by`` set (the P2 beat has no principal). Sends go
through ``time_service.remind`` only -- no second reminder path; its 4 h gap still applies.
"""

import dataclasses
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approvals import ApprovalRequest
from app.models.placements import Placement
from app.models.tasks import Task
from app.models.time import Timesheet, TimesheetReminderSent
from app.schemas.timesheet_flags import NudgeItem, NudgePlan

SUBMIT_AFTER_DAYS = 2
SUBMIT_EVERY_DAYS = 2
SUBMIT_MAX = 4
APPROVE_AFTER_DAYS = 2
APPROVE_EVERY_DAYS = 2
ESCALATE_AFTER_DAYS = 3
RECENT_HOURS = 20
SUBMIT_STATUSES = ("draft", "rejected")
APPROVE_STATUSES = ("submitted", "in_approval")


def business_days_between(start: date, end: date) -> int:
    """Mon-Fri days after ``start`` up to and including ``end`` (the P2 ``calendar`` rule)."""
    if end <= start:
        return 0
    n, d = 0, start + timedelta(days=1)
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


@dataclasses.dataclass(frozen=True, slots=True)
class NudgeFacts:
    timesheet_id: uuid.UUID
    human_id: str
    status: str
    period_start: date
    period_end: date
    placement_id: uuid.UUID
    candidate_id: uuid.UUID
    step_since: date | None = None  # date the current approval step became active
    approver_step: str | None = None
    step_key: str | None = None  # "<request>:<position>"
    recipient_kind: str | None = None
    recipient_id: uuid.UUID | None = None
    last_reminder_at: datetime | None = None
    last_agent_nudge_on: date | None = None
    agent_nudges: int = 0
    escalated: bool = False


def _item(f: NudgeFacts, kind: str, age: int, *, escalate: bool = False) -> NudgeItem:
    return NudgeItem(
        timesheet_id=f.timesheet_id, human_id=f.human_id, kind=kind, status=f.status,
        period_start=f.period_start, period_end=f.period_end, age_business_days=age, nudges_sent=f.agent_nudges,
        recipient_kind=f.recipient_kind, recipient_id=f.recipient_id, placement_id=f.placement_id,
        candidate_id=f.candidate_id, escalate=escalate, approver_step=f.approver_step,
    )  # fmt: skip


def plan(am_id: uuid.UUID, facts: Iterable[NudgeFacts], today: date, now: datetime) -> NudgePlan:
    """Pure: the nudge plan of one Account Manager for ``today`` (``now`` is the 20 h reference)."""
    out = NudgePlan(account_manager_id=am_id, today=today)
    recent = now - timedelta(hours=RECENT_HOURS)
    for f in facts:
        reminded_recently = f.last_reminder_at is not None and f.last_reminder_at > recent
        if f.status in SUBMIT_STATUSES:
            age = business_days_between(f.period_end, today)
            if age < SUBMIT_AFTER_DAYS:
                continue
            if f.agent_nudges >= SUBMIT_MAX:
                out.max_reached.append(_item(f, "submit", age))
                continue
            due = f.last_agent_nudge_on is None or business_days_between(f.last_agent_nudge_on, today) >= SUBMIT_EVERY_DAYS
            if due and not reminded_recently and f.recipient_id is not None:
                out.submit.append(_item(f, "submit", age))
        elif f.status in APPROVE_STATUSES:
            since = f.step_since or f.period_end
            age = business_days_between(since, today)
            if age >= ESCALATE_AFTER_DAYS and not f.escalated:
                out.escalate.append(_item(f, "approve", age, escalate=True))
            if age < APPROVE_AFTER_DAYS:
                continue
            due = f.last_agent_nudge_on is None or business_days_between(f.last_agent_nudge_on, today) >= APPROVE_EVERY_DAYS
            if due and not reminded_recently and f.recipient_id is not None:
                out.approve.append(_item(f, "approve", age, escalate=age >= ESCALATE_AFTER_DAYS and not f.escalated))
    return out


# ---------------------------------------------------------------- loader
def escalation_marker(timesheet_id: uuid.UUID, step_key: str | None) -> str:
    return f"[ts-escalation:{timesheet_id}:{step_key or '-'}]"


async def facts_for(
    db: AsyncSession,
    *,
    account_manager_id: uuid.UUID | None = None,
    timesheet_ids: Sequence[uuid.UUID] | None = None,
) -> tuple[list[NudgeFacts], dict[uuid.UUID, uuid.UUID]]:
    """Facts of the late / pending regular timesheets of an AM's placements (or of ``timesheet_ids``) and each
    timesheet's AM."""
    stmt = (
        select(Timesheet, Placement.account_manager_user_id)
        .join(Placement, Placement.id == Timesheet.placement_id)
        .where(Timesheet.kind == "regular", Timesheet.status.in_((*SUBMIT_STATUSES, *APPROVE_STATUSES)))
    )
    if account_manager_id is not None:
        stmt = stmt.where(Placement.account_manager_user_id == account_manager_id)
    if timesheet_ids is not None:
        stmt = stmt.where(Timesheet.id.in_(list(timesheet_ids) or [uuid.uuid4()]))
    rows = (await db.execute(stmt.order_by(Timesheet.period_end, Timesheet.human_id).limit(500))).all()
    ids = [ts.id for ts, _ in rows]
    am_of = {ts.id: am for ts, am in rows if am is not None}
    if not ids:
        return [], am_of
    last = dict(
        (
            await db.execute(
                select(TimesheetReminderSent.timesheet_id, func.max(TimesheetReminderSent.sent_at))
                .where(TimesheetReminderSent.timesheet_id.in_(ids))
                .group_by(TimesheetReminderSent.timesheet_id)
            )
        ).tuples().all()
    )
    agent_rows = (
        await db.execute(
            select(TimesheetReminderSent.timesheet_id, func.count(), func.max(TimesheetReminderSent.sent_at))
            .where(
                TimesheetReminderSent.timesheet_id.in_(ids), TimesheetReminderSent.kind == "scheduled",
                TimesheetReminderSent.sent_by.is_not(None),
            )
            .group_by(TimesheetReminderSent.timesheet_id)
        )
    ).all()  # fmt: skip
    agent = {r[0]: (int(r[1]), r[2]) for r in agent_rows}
    req_ids = [ts.approval_request_id for ts, _ in rows if ts.approval_request_id is not None]
    requests = (
        {r.id: r for r in (await db.execute(select(ApprovalRequest).where(ApprovalRequest.id.in_(req_ids)))).scalars()}
        if req_ids
        else {}
    )
    markers = list(
        (
            await db.execute(
                select(Task.notes).where(Task.entity_type == "placement", Task.notes.like("%[ts-escalation:%"))
            )
        ).scalars()
    )
    marker_text = "\n".join(m or "" for m in markers)
    out: list[NudgeFacts] = []
    for ts, _am in rows:
        recipient: tuple[str, uuid.UUID] | None = None
        step = step_key = None
        since: date | None = None
        if ts.status in SUBMIT_STATUSES:
            recipient = ("worker", ts.candidate_id)
        else:
            req = requests.get(ts.approval_request_id) if ts.approval_request_id else None
            if req is not None:
                if req.current_approver_user_id is not None:
                    recipient = ("user", req.current_approver_user_id)
                elif req.current_approver_client_user_id is not None:
                    recipient = ("client_user", req.current_approver_client_user_id)
                snap = next(
                    (s for s in req.chain_snapshot or [] if int(s.get("position", -1)) == (req.current_position or -2)),
                    None,
                )
                step = str(snap.get("name") or snap.get("label") or "") if snap else None
                step_key = f"{req.id}:{req.current_position}"
                activated = req.current_step_activated_at or req.submitted_at
                since = activated.date() if activated else None
            if since is None and ts.submitted_at is not None:
                since = ts.submitted_at.date()
        count, last_agent = agent.get(ts.id, (0, None))
        out.append(
            NudgeFacts(
                timesheet_id=ts.id, human_id=ts.human_id, status=ts.status, period_start=ts.period_start,
                period_end=ts.period_end, placement_id=ts.placement_id, candidate_id=ts.candidate_id,
                step_since=since, approver_step=step or None, step_key=step_key,
                recipient_kind=recipient[0] if recipient else None, recipient_id=recipient[1] if recipient else None,
                last_reminder_at=last.get(ts.id), last_agent_nudge_on=last_agent.date() if last_agent else None,
                agent_nudges=count, escalated=escalation_marker(ts.id, step_key) in marker_text,
            )
        )  # fmt: skip
    return out, am_of


async def plan_for(
    db: AsyncSession, account_manager_id: uuid.UUID, today: date, now: datetime | None = None
) -> NudgePlan:
    facts, _ = await facts_for(db, account_manager_id=account_manager_id)
    return plan(account_manager_id, facts, today, now or datetime.now(UTC))


async def account_managers_with_items(db: AsyncSession) -> list[uuid.UUID]:
    rows = (
        await db.execute(
            select(Placement.account_manager_user_id)
            .join(Timesheet, Timesheet.placement_id == Placement.id)
            .where(
                Timesheet.kind == "regular", Timesheet.status.in_((*SUBMIT_STATUSES, *APPROVE_STATUSES)),
                Placement.account_manager_user_id.is_not(None),
            )
            .distinct()
        )
    ).scalars()  # fmt: skip
    return sorted(set(rows), key=str)


__all__ = [
    "APPROVE_AFTER_DAYS",
    "ESCALATE_AFTER_DAYS",
    "SUBMIT_EVERY_DAYS",
    "SUBMIT_MAX",
    "NudgeFacts",
    "account_managers_with_items",
    "business_days_between",
    "escalation_marker",
    "facts_for",
    "plan",
    "plan_for",
]
