# ruff: file-ignore[line-too-long]
"""Compliance Agent tools (Epic 62 A4; ``owner_epic="62"``, ``version=1``).

| Key | Risk | Notes |
|---|---|---|
| ``compliance.get_chase_plan`` | read | the deterministic plan (structured only) |
| ``compliance.send_chase_reminder`` | low (auto); high with ``custom_note`` | templated reminder to the Worker on record; re-validated against today's plan (``item_not_due``) |
| ``compliance.escalate_chase`` | low (auto) | Task to the onboarding coordinator + ``ChaseEscalated`` |
| ``compliance.get_risk_list`` | read | pre-start risk list within the owner's data scope |

★ never: waive a checklist item, verify / reject a credential or document, override a start gate, mark Joined, edit a
Worker's personal, bank or tax data -- no such tool exists. Recipients are the run's Worker only (no address argument).
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.agentic import (
    ProposalPreview,
    Recipient,
    ScopeRule,
    SendIntent,
    ToolOutcome,
    ToolSpec,
    register_tool,
)
from app.core.agentic.types import RiskClass
from app.core.errors import NotFound
from app.services import agent_runtime_service as runtime
from app.services import chasing_service, entity_refs

OWNER_EPIC = "62"
CANDIDATE = ScopeRule(resource="candidate", target_arg="candidate_id", candidate_arg="candidate_id")
Channel = Literal["portal", "email", "sms", "whatsapp"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _scoped(tctx: Any) -> Any:
    return runtime.scope_context(tctx.request, tctx.owner_user_id)


async def _readable(tctx: Any, candidate_id: uuid.UUID) -> None:
    if not await entity_refs.can_read(_scoped(tctx), tctx.db, "candidate", candidate_id):
        raise NotFound("Resource not found")


async def _worker_name(tctx: Any, candidate_id: uuid.UUID) -> str:
    who = await entity_refs.addressee(tctx.db, "candidate", candidate_id)
    return (who.full_name if who is not None and who.full_name else None) or "the worker"


# ================================================================ compliance.get_chase_plan
class CandidateIn(_In):
    candidate_id: uuid.UUID


class PlanItem(_Out):
    item_kind: Literal["checklist_item", "credential", "permit"]
    item_ref: uuid.UUID
    label: str
    due_basis: Literal["start_date", "expiry"]
    due_on: str
    days_left: int
    offset_days: int
    placement_id: uuid.UUID | None


class ChannelGap(_Out):
    channel: str
    reason: str


class PlanOut(_Out):
    candidate_id: uuid.UUID
    today: str
    items: list[PlanItem]
    allowed_channels: list[str]
    unavailable_channels: list[ChannelGap]
    last_contact: str | None
    portal_last_seen: str | None
    escalate: bool
    escalation_reasons: list[str]
    coordinator_id: uuid.UUID | None
    placement_ids: list[uuid.UUID]


async def _plan(tctx: Any, args: CandidateIn) -> ToolOutcome:
    await _readable(tctx, args.candidate_id)
    view = await chasing_service.chases.plan_view(tctx.db, args.candidate_id)
    view.pop("skipped", None)
    data = PlanOut.model_validate(view)
    refs: list[tuple[str, uuid.UUID]] = [("candidate", args.candidate_id)]
    refs += [("placement", p) for p in data.placement_ids]
    refs += [(i.item_kind, i.item_ref) for i in data.items]
    return ToolOutcome(
        data, refs=tuple(refs), candidate_ids=(args.candidate_id,), target=("candidate", args.candidate_id),
        summary=f"{len(data.items)} items due" + (", escalate" if data.escalate else ""),
    )  # fmt: skip


# ================================================================ compliance.send_chase_reminder
class ReminderItem(_In):
    item_kind: Literal["checklist_item", "credential", "permit"]
    item_ref: uuid.UUID
    offset_days: int = Field(ge=-30, le=30)


class ReminderIn(_In):
    candidate_id: uuid.UUID
    items: list[ReminderItem] = Field(min_length=1, max_length=10)
    channels: list[Channel] = Field(min_length=1, max_length=3)
    custom_note: str | None = Field(default=None, max_length=300)


class Suppressed(_Out):
    channel: str
    reason: str


class ReminderOut(_Out):
    chase_ids: list[uuid.UUID]
    sent: list[str]
    suppressed: list[Suppressed]


def _reminder_risk(args: Any) -> RiskClass:
    return "high" if getattr(args, "custom_note", None) else "low"


async def _reminder_intent(tctx: Any, args: ReminderIn) -> SendIntent:
    await _readable(tctx, args.candidate_id)
    return SendIntent(
        recipients=tuple(Recipient(kind="worker", id=args.candidate_id, channel=ch) for ch in dict.fromkeys(args.channels)),
        templated=args.custom_note is None, template_ref="agent_document_reminder", text_for_checks=args.custom_note or "",
    )  # fmt: skip


async def _reminder(tctx: Any, args: ReminderIn) -> ToolOutcome:
    await _readable(tctx, args.candidate_id)
    note = None
    if args.custom_note:
        from app.services.agent_runtime.sanitize import outbound

        note = outbound(args.custom_note, max_len=300).text
    out = await chasing_service.chases.send_reminder(
        tctx.request, tctx.db, args.candidate_id, [(i.item_kind, i.item_ref, i.offset_days) for i in args.items],
        list(args.channels), note, run_id=tctx.run_id, owner_user_id=tctx.owner_user_id,
    )  # fmt: skip
    data = ReminderOut(
        chase_ids=out["chase_ids"], sent=out["sent"], suppressed=[Suppressed(**s) for s in out["suppressed"]]
    )
    summary = "reminder sent via " + ", ".join(data.sent) if data.sent else "reminder suppressed"
    if data.suppressed:
        summary += "; " + ", ".join(f"suppressed:{s.reason}" for s in data.suppressed)
    return ToolOutcome(
        data, refs=tuple(("compliance_chase", c) for c in data.chase_ids), candidate_ids=(args.candidate_id,),
        target=("candidate", args.candidate_id), summary=summary[:500],
    )  # fmt: skip


async def _reminder_preview(tctx: Any, args: ReminderIn) -> ProposalPreview:
    name = await _worker_name(tctx, args.candidate_id)
    try:
        r = await chasing_service.chases.compute(tctx.db, args.candidate_id)
        labels = {i.item_ref: i.label for i in r.plan.items} | r.bundle.labels
    except Exception:
        labels = {}
    items = ", ".join(labels.get(i.item_ref, "an item") for i in args.items)
    fields: list[tuple[str, str]] = [
        ("Worker", name),
        ("Channels", ", ".join(args.channels)),
        ("Items", items),
    ]
    if args.custom_note:
        fields.append(("Personal note", args.custom_note))
    return ProposalPreview(
        title=f"Send document reminder to {name}: {items}"[:200],
        summary=("Templated reminder \"Agent · Document reminder\"" + (f" with the note: {args.custom_note}" if args.custom_note else ""))[:4000],
        fields=tuple(fields), renderer="compliance.send_chase_reminder",
    )  # fmt: skip


# ================================================================ compliance.escalate_chase
class EscalateIn(_In):
    candidate_id: uuid.UUID
    placement_id: uuid.UUID | None = None
    reason: Literal["portal_not_opened", "deadline_close", "repeated_no_response"]


class EscalateOut(_Out):
    task_id: uuid.UUID


async def _escalate(tctx: Any, args: EscalateIn) -> ToolOutcome:
    await _readable(tctx, args.candidate_id)
    task_id = await chasing_service.chases.escalate(
        tctx.request, tctx.db, args.candidate_id, args.placement_id, args.reason, run_id=tctx.run_id
    )
    return ToolOutcome(
        EscalateOut(task_id=task_id), refs=(("task", task_id),), candidate_ids=(args.candidate_id,),
        target=("candidate", args.candidate_id), summary=f"escalated ({args.reason})",
    )  # fmt: skip


async def _escalate_preview(tctx: Any, args: EscalateIn) -> ProposalPreview:
    name = await _worker_name(tctx, args.candidate_id)
    label = chasing_service.chases.REASON_LABELS.get(args.reason, args.reason)
    return ProposalPreview(
        title=f"Escalate {name} to the onboarding coordinator ({label})"[:200],
        summary="Create a task for the coordinator to chase the missing compliance items.",
        fields=(("Worker", name), ("Reason", label)),
        renderer="compliance.escalate_chase",
    )


# ================================================================ compliance.get_risk_list
class RiskIn(_In):
    days: int = Field(default=14, ge=1, le=30)
    limit: int = Field(default=20, ge=1, le=50)


class RiskRowOut(_Out):
    placement_id: uuid.UUID
    candidate_id: uuid.UUID
    start_date: str
    days_to_start: int
    missing_items: list[str]
    portal_opened: bool
    chases_sent: int
    escalated: bool
    bgv_status: str | None


class RiskOut(_Out):
    rows: list[RiskRowOut]


async def _risk(tctx: Any, args: RiskIn) -> ToolOutcome:
    data = await chasing_service.risk_list(_scoped(tctx), tctx.db, days=args.days, limit=args.limit)
    rows = [
        RiskRowOut(
            placement_id=r.placement_id,
            candidate_id=r.candidate_id,
            start_date=r.start_date.isoformat(),
            days_to_start=r.days_to_start,
            missing_items=[m.label for m in r.missing_items],
            portal_opened=r.portal_last_seen is not None,
            chases_sent=r.chases_sent,
            escalated=r.escalated,
            bgv_status=r.bgv_status,
        )
        for r in data.rows
    ]
    refs = [("placement", r.placement_id) for r in rows] + [("candidate", r.candidate_id) for r in rows]
    return ToolOutcome(
        RiskOut(rows=rows),
        refs=tuple(refs),
        candidate_ids=tuple(r.candidate_id for r in rows),
        summary=f"{len(rows)} placements at risk",
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        key="compliance.get_chase_plan", version=1, owner_epic=OWNER_EPIC, title="Read the chase plan",
        description="Today's deterministic chase plan for one Worker: items due a reminder (label, basis, due date, days left, cadence offset), the channels that may be used, last contact, portal last seen and whether to escalate. Read-only.",
        input_model=CandidateIn, output_model=PlanOut, permissions=("compliance:read", "placement:read"), risk="read",
        scope=CANDIDATE, handler=_plan,
    ),
    ToolSpec(
        key="compliance.send_chase_reminder", version=1, owner_epic=OWNER_EPIC, title="Send a document reminder",
        description="Send ONE templated document reminder to the Worker for the items the plan lists as due today (use the plan's item_kind, item_ref and offset_days), on the channels the plan allows. A custom_note (max 300 chars) turns it into a proposal for a person. Items not due today are refused (item_not_due).",
        input_model=ReminderIn, output_model=ReminderOut, permissions=("compliance:read", "message:send_templated"), risk="low",
        scope=CANDIDATE, handler=_reminder, preview=_reminder_preview, risk_fn=_reminder_risk, egress=True,
        send_intent=_reminder_intent, default_mode="auto", max_calls_per_run=2,
    ),
    ToolSpec(
        key="compliance.escalate_chase", version=1, owner_epic=OWNER_EPIC, title="Escalate to the coordinator",
        description="Create a task for the Worker's onboarding coordinator when the plan says escalate (portal_not_opened, deadline_close) or reminders got no response. One task per Worker, reason and day.",
        input_model=EscalateIn, output_model=EscalateOut, permissions=("task:create", "compliance:read"), risk="low",
        scope=CANDIDATE, handler=_escalate, preview=_escalate_preview, default_mode="auto", max_calls_per_run=2,
    ),
    ToolSpec(
        key="compliance.get_risk_list", version=1, owner_epic=OWNER_EPIC, title="Read the pre-start risk list",
        description="Placements starting within N days (max 30) with missing mandatory compliance items, within your data scope: missing items, whether the portal was opened, reminders sent, escalation and BGV status. Read-only.",
        input_model=RiskIn, output_model=RiskOut, permissions=("compliance:read", "placement:read"), risk="read",
        scope=ScopeRule(resource="none", cross_subject=True), handler=_risk,
    ),
)  # fmt: skip


def register() -> None:
    from app.core.agentic.tools import tools

    for spec in TOOLS:
        if spec.key not in tools():
            register_tool(spec)


register()
