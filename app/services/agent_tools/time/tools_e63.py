# ruff: noqa: E501, D401
"""Epic 63 Timesheet Agent tools (``owner_epic="63"``, ``version=1``). None of them approves, rejects or reopens a
timesheet, edits hours or entries, creates a proxy entry, or adds / removes flags: those stay human (P2 FR-131 / the
``time.timesheets`` approval path is never imported).

| Key | Risk | Notes |
|---|---|---|
| ``time.get_nudge_plan`` | read | the Account Manager's plan today (submit / approve / escalate) |
| ``time.get_timesheet`` | read | period, status, totals, days with leave / holiday markers, approver step, age -- **no entry notes** |
| ``time.get_context`` | read | work days, thresholds, cap, approved leave, holidays, last 8 periods' OT, peers (median / MAD), PO hours left |
| ``time.list_flags`` | read | the rule flags of the timesheet |
| ``time.send_timesheet_reminder`` | low (auto) | egress: P2 ``remind(kind="scheduled")`` for planned items only; recipients from P2 records |
| ``time.save_flag_explanations`` | low (auto) | explanations + group keys; numbers must come from the flag facts |
| ``time.escalate_approval`` | low (auto) | one Task per approval step for the AM (on the Placement) |

Deviation (recorded): runtime idempotency keys are built from the arguments only, so the reminder uses per-run keys;
the cross-run "once per item per 4 hours / not on a day P2 reminded" rule is P2 ``remind``'s gap plus the plan's 20 h
window. ``escalate_approval`` dedupes per approval step in the service.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.agentic import (
    IdempotencyRule,
    ProposalPreview,
    Recipient,
    ScopeRule,
    SendIntent,
    ToolOutcome,
    ToolSpec,
    register_tool,
)
from app.core.errors import NotFound
from app.core.permissions import Scope
from app.schemas.timesheet_flags import FlagOut, NudgePlan
from app.services import agent_runtime_service as runtime

OWNER_EPIC = "63"
VERSION = 1


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _svc() -> Any:
    from app.services import timesheet_flags_service

    return timesheet_flags_service


def _scoped(tctx: Any) -> Any:
    return runtime.scope_context(tctx.request, tctx.owner_user_id)


def _tenant_wide(tctx: Any, permission: str) -> bool:
    principal = _scoped(tctx).principal
    perms = getattr(principal, "permissions", None) or {}
    value = perms.get(permission)
    return value is not None and Scope(value) == Scope.TENANT


async def _today(tctx: Any) -> tuple[date, str]:
    from app.services import entity_refs

    tenant = await entity_refs.tenant(tctx.db, entity_refs.session_tenant(tctx.db))
    return datetime.now(tenant.tz).date(), str(tenant.tz)


async def _timesheet(tctx: Any, timesheet_id: uuid.UUID) -> Any:
    """The timesheet when the owner may see it: tenant-wide ``timesheet:read`` or the Placement's AM."""
    from app.models.time import Timesheet

    ts = await tctx.db.get(Timesheet, timesheet_id)
    if ts is None:
        raise NotFound("Timesheet not found")
    if not _tenant_wide(tctx, "timesheet:read") and await _svc().am_of(tctx.db, ts.id) != tctx.owner_user_id:
        raise NotFound("Timesheet not found")
    return ts


# ================================================================ time.get_nudge_plan
class PlanIn(_In):
    account_manager_id: uuid.UUID


async def _plan(tctx: Any, args: PlanIn) -> ToolOutcome:
    if args.account_manager_id != tctx.owner_user_id and not _tenant_wide(tctx, "timesheet:monitor"):
        raise NotFound("Account manager not found")
    today, _ = await _today(tctx)
    plan: NudgePlan = await _svc().plan_for(tctx.db, args.account_manager_id, today)
    refs: list[tuple[str, uuid.UUID]] = []
    for item in [*plan.submit, *plan.approve, *plan.escalate, *plan.max_reached]:
        refs += [("timesheet", item.timesheet_id), ("placement", item.placement_id)]
        if item.recipient_id is not None:
            refs.append((item.recipient_kind or "user", item.recipient_id))
    return ToolOutcome(
        plan, refs=tuple(dict.fromkeys(refs)), target=("user", args.account_manager_id),
        summary=f"plan: {len(plan.submit)} submit, {len(plan.approve)} approve, {len(plan.escalate)} escalate",
    )  # fmt: skip


# ================================================================ time.get_timesheet
class TimesheetIn(_In):
    timesheet_id: uuid.UUID


class Day(_Out):
    date: date
    hours: float
    category: str
    leave: bool
    holiday: bool


class TimesheetOut(_Out):
    timesheet_id: uuid.UUID
    human_id: str
    period_start: date
    period_end: date
    status: str
    totals_by_category: dict[str, float]
    days: list[Day]
    approver_step: str | None
    age_business_days: int


async def _get_timesheet(tctx: Any, args: TimesheetIn) -> ToolOutcome:
    from app.services.timesheet_flags import nudges

    ts = await _timesheet(tctx, args.timesheet_id)
    _, inp = await _svc().timesheet_context(tctx.db, ts.id)
    totals: dict[str, Decimal] = {}
    days = []
    for e in sorted(inp.entries, key=lambda e: (e.day, e.category)):
        totals[e.category] = totals.get(e.category, Decimal(0)) + e.hours
        days.append(Day(date=e.day, hours=float(e.hours), category=e.category, leave=e.day in inp.leave, holiday=e.day in inp.holidays))
    facts, _ = await nudges.facts_for(tctx.db, timesheet_ids=[ts.id])
    f = facts[0] if facts else None
    today, _tz = await _today(tctx)
    anchor = (f.step_since if f and f.step_since else ts.period_end)
    out = TimesheetOut(
        timesheet_id=ts.id, human_id=ts.human_id, period_start=ts.period_start, period_end=ts.period_end, status=ts.status,
        totals_by_category={k: float(v) for k, v in totals.items()}, days=days, approver_step=f.approver_step if f else None,
        age_business_days=nudges.business_days_between(anchor, today),
    )  # fmt: skip
    return ToolOutcome(
        out, refs=(("placement", ts.placement_id),), target=("timesheet", ts.id), summary=f"timesheet {ts.human_id} ({ts.status})"
    )


# ================================================================ time.get_context
class ContextOut(_Out):
    timesheet_id: uuid.UUID
    work_days: list[int]
    hours_per_day: float
    daily_cap: float
    approved_leave_days: list[date]
    half_leave_days: list[date]
    holidays: list[date]
    last_periods_overtime_hours: list[float]
    peer_count: int
    peer_median_hours: float | None
    peer_mad_hours: float | None
    po_remaining_hours: float | None


async def _get_context(tctx: Any, args: TimesheetIn) -> ToolOutcome:
    from app.services.timesheet_flags import rules

    ts = await _timesheet(tctx, args.timesheet_id)
    _, inp = await _svc().timesheet_context(tctx.db, ts.id)
    peers = list(inp.peer_totals)
    out = ContextOut(
        timesheet_id=ts.id, work_days=sorted(inp.work_days), hours_per_day=float(inp.hours_per_day), daily_cap=float(inp.daily_cap),
        approved_leave_days=sorted(d for d, f in inp.leave.items() if f >= 1),
        half_leave_days=sorted(d for d, f in inp.leave.items() if f < 1), holidays=sorted(inp.holidays),
        last_periods_overtime_hours=[float(v) for v in inp.history_ot], peer_count=len(peers),
        peer_median_hours=float(rules.median(peers)) if len(peers) >= rules.PEER_MIN else None,
        peer_mad_hours=float(rules.mad(peers)) if len(peers) >= rules.PEER_MIN else None,
        po_remaining_hours=float(inp.po_remaining_hours) if inp.po_remaining_hours is not None else None,
    )  # fmt: skip
    return ToolOutcome(out, target=("timesheet", ts.id), summary="timesheet context read")


# ================================================================ time.list_flags
class FlagsOut(_Out):
    flags: list[FlagOut]


async def _list_flags(tctx: Any, args: TimesheetIn) -> ToolOutcome:
    ts = await _timesheet(tctx, args.timesheet_id)
    flags = await _svc().list_for_timesheet(_scoped(tctx), tctx.db, ts.id)
    return ToolOutcome(FlagsOut(flags=flags), target=("timesheet", ts.id), summary=f"{len(flags)} flags")


# ================================================================ time.send_timesheet_reminder
class ReminderIn(_In):
    timesheet_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)


class Skipped(_Out):
    id: str
    reason: str


class ReminderOut(_Out):
    sent: list[uuid.UUID]
    skipped: list[Skipped]


async def _reminder_intent(tctx: Any, args: ReminderIn) -> SendIntent:
    from app.services.timesheet_flags import nudges

    facts, am_by_ts = await nudges.facts_for(tctx.db, timesheet_ids=list(args.timesheet_ids))
    recipients: list[Recipient] = []
    for f in facts:
        if am_by_ts.get(f.timesheet_id) != tctx.owner_user_id and not _tenant_wide(tctx, "timesheet:monitor"):
            raise NotFound("Timesheet not found")
        if f.recipient_id is None or f.recipient_kind is None:
            continue
        kind = "worker" if f.recipient_kind == "worker" else f.recipient_kind
        recipients.append(Recipient(kind=kind, id=f.recipient_id, channel="email"))  # type: ignore[arg-type]
    if len(facts) != len(set(args.timesheet_ids)):
        raise NotFound("Timesheet not found")
    return SendIntent(recipients=tuple(dict.fromkeys(recipients)), templated=True, template_ref="time.timesheet_reminder")


async def _reminder(tctx: Any, args: ReminderIn) -> ToolOutcome:
    today, _ = await _today(tctx)
    sent, skipped = await _svc().send_reminders(tctx.request, tctx.db, tctx.owner_user_id, list(args.timesheet_ids), today)
    return ToolOutcome(
        ReminderOut(sent=sent, skipped=[Skipped(**s) for s in skipped]),
        refs=tuple(("timesheet", t) for t in sent), summary=f"{len(sent)} reminders sent, {len(skipped)} skipped",
    )


async def _reminder_preview(tctx: Any, args: ReminderIn) -> ProposalPreview:
    from app.services.timesheet_flags import nudges

    facts, _ = await nudges.facts_for(tctx.db, timesheet_ids=list(args.timesheet_ids))
    names = ", ".join(f.human_id for f in facts[:10])
    return ProposalPreview(
        title=f"Send the standard timesheet reminder for {len(facts)} timesheet(s)"[:200],
        summary=f"Templated P2 reminders (submit or approve) for: {names}. At most one per timesheet every 4 hours.",
        fields=(("Timesheets", names), ("Template", "Timesheet reminder")), renderer="time.send_timesheet_reminder",
    )


# ================================================================ time.save_flag_explanations
class ExplanationItem(_In):
    flag_id: uuid.UUID
    explanation: str = Field(min_length=1, max_length=600)
    group_key: str | None = Field(default=None, max_length=40, pattern=r"^[a-z0-9_]{1,40}$")


class ExplanationsIn(_In):
    timesheet_id: uuid.UUID
    items: list[ExplanationItem] = Field(min_length=1, max_length=20)


class ExplanationsOut(_Out):
    updated: int


async def _explanations(tctx: Any, args: ExplanationsIn) -> ToolOutcome:
    from app.schemas.timesheet_flags import FlagExplanationIn
    from app.services.agent_runtime.sanitize import outbound

    ts = await _timesheet(tctx, args.timesheet_id)
    items = [
        FlagExplanationIn(flag_id=i.flag_id, explanation=outbound(i.explanation, max_len=600).text or i.explanation[:600], group_key=i.group_key)
        for i in args.items
    ]
    n = await _svc().set_explanations(tctx.request, tctx.db, ts.id, items, agent_run_id=tctx.run_id)
    return ToolOutcome(ExplanationsOut(updated=n), target=("timesheet", ts.id), summary=f"{n} explanations saved")


async def _explanations_preview(tctx: Any, args: ExplanationsIn) -> ProposalPreview:  # noqa: ARG001
    return ProposalPreview(
        title="Save plain-English explanations of the timesheet flags",
        summary="\n".join(f"- {i.explanation}" for i in args.items)[:4000],
        fields=(("Flags", str(len(args.items))),), renderer="time.save_flag_explanations",
    )


# ================================================================ time.escalate_approval
class EscalateIn(_In):
    timesheet_id: uuid.UUID
    days_pending: int = Field(ge=0, le=365)


class EscalateOut(_Out):
    task_id: uuid.UUID


async def _escalate(tctx: Any, args: EscalateIn) -> ToolOutcome:
    ts = await _timesheet(tctx, args.timesheet_id)
    task_id = await _svc().escalate(tctx.request, tctx.db, tctx.owner_user_id, ts.id, args.days_pending)
    return ToolOutcome(
        EscalateOut(task_id=task_id), refs=(("task", task_id),), target=("timesheet", ts.id),
        summary=f"escalation task for {ts.human_id}",
    )


async def _escalate_preview(tctx: Any, args: EscalateIn) -> ProposalPreview:
    from app.models.time import Timesheet

    ts = await tctx.db.get(Timesheet, args.timesheet_id)
    label = ts.human_id if ts is not None else str(args.timesheet_id)
    return ProposalPreview(
        title=f"Task for the Account Manager: timesheet {label} waiting {args.days_pending} days"[:200],
        summary="Creates one Task per approval step so the Account Manager can chase the approver.",
        fields=(("Timesheet", label), ("Days pending", str(args.days_pending))), renderer="time.escalate_approval",
    )


TS_SCOPE = ScopeRule(resource="timesheet", target_arg="timesheet_id")
RiskLiteral = Literal["read", "low", "high"]

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        key="time.get_nudge_plan", version=VERSION, owner_epic=OWNER_EPIC, title="Timesheet nudge plan",
        description="Read today's timesheet nudge plan of an Account Manager: drafts / rejected timesheets to remind the Worker about, pending approvals to remind the approver about, approvals to escalate, and items that reached the reminder limit. P2 already reminds on days 0 and 1; the plan never repeats a reminder sent in the last 20 hours. Read only.",
        input_model=PlanIn, output_model=NudgePlan, permissions=("timesheet:monitor",), risk="read",
        scope=ScopeRule(resource="user", target_arg="account_manager_id"), handler=_plan,
    ),
    ToolSpec(
        key="time.get_timesheet", version=VERSION, owner_epic=OWNER_EPIC, title="Timesheet summary",
        description="Read a timesheet: human id, period, status, totals by category, each day's hours and category with leave / holiday markers, the current approver step and its age in business days. Entry notes are not included. Read only.",
        input_model=TimesheetIn, output_model=TimesheetOut, permissions=("timesheet:read",), risk="read",
        scope=TS_SCOPE, handler=_get_timesheet,
    ),
    ToolSpec(
        key="time.get_context", version=VERSION, owner_epic=OWNER_EPIC, title="Timesheet context",
        description="Read the facts behind a timesheet's anomaly checks: work days, standard day and daily cap, approved leave and holidays in the period, overtime of the last 8 approved periods, project peers (count, median, MAD) and PO hours left. Read only.",
        input_model=TimesheetIn, output_model=ContextOut, permissions=("timesheet:read", "placement:read"), risk="read",
        scope=TS_SCOPE, handler=_get_context,
    ),
    ToolSpec(
        key="time.list_flags", version=VERSION, owner_epic=OWNER_EPIC, title="Timesheet flags",
        description="List the deterministic anomaly flags the rules engine raised on a timesheet (rule, severity, dates, numeric facts, status). Read only.",
        input_model=TimesheetIn, output_model=FlagsOut, permissions=("timesheet:read",), risk="read",
        scope=TS_SCOPE, handler=_list_flags,
    ),
    ToolSpec(
        key="time.send_timesheet_reminder", version=VERSION, owner_epic=OWNER_EPIC, title="Send timesheet reminders",
        description="Send the firm's standard timesheet reminder (through the P2 reminder path) for up to 20 timesheets in today's nudge plan: the Worker for drafts / rejected timesheets, the current approver (user or client user) for pending ones. Recipients come from the records; there is no address argument. Items not in the plan or reminded in the last 4 hours are skipped.",
        input_model=ReminderIn, output_model=ReminderOut, permissions=("timesheet:monitor", "message:send_templated"), risk="low",
        scope=ScopeRule(resource="timesheet", cross_subject=True), handler=_reminder, preview=_reminder_preview,
        default_mode="auto", egress=True, send_intent=_reminder_intent,
        idempotency=IdempotencyRule(fields=("timesheet_ids",), per_run=True), max_calls_per_run=2,
    ),
    ToolSpec(
        key="time.save_flag_explanations", version=VERSION, owner_epic=OWNER_EPIC, title="Save flag explanations",
        description="Save one plain sentence (at most 600 characters) per flag explaining the fact and why it may matter, with a shared group_key for related flags (e.g. leave). Use only numbers and dates present in the flag facts or context; the server refuses others. Never call a timesheet wrong or fraudulent.",
        input_model=ExplanationsIn, output_model=ExplanationsOut, permissions=("timesheet:read",), risk="low",
        scope=TS_SCOPE, handler=_explanations, preview=_explanations_preview, default_mode="auto",
        untrusted_input=frozenset(), max_calls_per_run=3,
    ),
    ToolSpec(
        key="time.escalate_approval", version=VERSION, owner_epic=OWNER_EPIC, title="Escalate a pending approval",
        description="Create a Task for the Account Manager that a timesheet has waited too long at its current approval step ('Timesheet {TS} waiting {n} days for {approver}'). One Task per approval step; a repeat returns the same Task.",
        input_model=EscalateIn, output_model=EscalateOut, permissions=("task:create", "timesheet:monitor"), risk="low",
        scope=ScopeRule(resource="timesheet", target_arg="timesheet_id", cross_subject=True), handler=_escalate,
        preview=_escalate_preview, default_mode="auto", idempotency=IdempotencyRule(fields=("timesheet_id",), per_run=True),
        max_calls_per_run=10,
    ),
)  # fmt: skip


def register() -> None:
    from app.core.agentic.tools import tools

    for spec in TOOLS:
        if spec.key not in tools():
            register_tool(spec)


register()
