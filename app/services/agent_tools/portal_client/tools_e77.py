# ruff: file-ignore[line-too-long]
"""Epic 77 Client Portal Copilot tools (``portal_client.*``, FR-396 / FR-397). Each wraps a client-portal service
(``demands.portal``, ``pipeline.portal``, ``interviews.portal``, ``placements.portal``, ``client_portal.approvals``,
``billing.portal``) acting as the Client User: delegations (``client_user_scopes``), FR-80 projections and org RLS
apply exactly as in the portal; "none visible" and "none exist" are the same answer. Every tool needs
``portal_copilot:use`` plus the portal permission of the endpoint it wraps (A0 table).

Never tools: approving or rejecting Timesheets, Expense Claims, Demands, deliverables or Candidates, interview
outcomes, offers -- ``portal_client.screen_for`` answers with the screen. Writes are Drafts the Client User confirms
(``portal_client.draft_demand_request``, ``portal_client.draft_submission_feedback``, ``portal_client.draft_message``).
"""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field

from app.core.agentic import ProposalPreview, ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.core.agentic.tools import tools as _registered
from app.services.agent_tools.portal_common import handoff
from app.services.agent_tools.portal_common.tooling import OWNER_EPIC, In, Out, acting, guard, session
from app.services.billing import portal as billing_portal
from app.services.client_portal import approvals as approvals_portal
from app.services.demands import portal as demands_portal
from app.services.interviews import portal as interviews_portal
from app.services.pipeline import portal as pipeline_portal
from app.services.placements import portal as placements_portal

KIND = "client"
USE = "portal_copilot:use"
MAX = 20
NONE = ScopeRule(resource="none")

# Static route map of the Client Portal (navigation only; ★ requests are answered with these screens).
SCREENS: dict[str, tuple[str, str, str]] = {
    "approve_timesheets": (
        "Open Approvals",
        "/client/approvals",
        "You can approve this in Approvals → Timesheets.",
    ),
    "approve_expenses": (
        "Open Approvals",
        "/client/approvals",
        "You can approve this in Approvals → Expense claims.",
    ),
    "approve_demand": ("Open Approvals", "/client/approvals", "You can approve this in Approvals."),
    "accept_deliverable": ("Open Projects", "/client/projects", "You can accept deliverables in Projects."),
    "reject_candidate": (
        "Open Demands",
        "/client/demands",
        "Decisions on candidates are yours to make on the submission screen.",
    ),
    "interview_outcome": (
        "Open Scorecards",
        "/client/scorecards",
        "Interview feedback is given on your scorecards.",
    ),
    "invoices": ("Open Invoices", "/client/invoices", "Your invoices are in Invoices."),
    "statement": ("Open Statement", "/client/statement", "Your account statement is in Statement."),
    "team": ("Open Team", "/client/team", "Your current workers are in Team."),
    "demands": ("Open Demands", "/client/demands", "Your requirements are in Demands."),
    "out_of_office": (
        "Open Out of office",
        "/client/delegation",
        "Delegate your approvals in Out of office.",
    ),
    "other": ("Open the portal", "/client", "I can't do this for you; you can do it yourself in the portal."),
}


# ================================================================ reads
class DemandsIn(In):
    status: str | None = Field(default=None, max_length=20)


class DemandOut(Out):
    id: uuid.UUID
    req_id: str
    title: str
    positions: int
    status: str
    location: str | None
    start_date: date | None
    sent_submissions: int
    url: str


class DemandsOut(Out):
    items: list[DemandOut]
    total: int


def _demand(d: Any) -> DemandOut:
    return DemandOut(
        id=d.id, req_id=d.req_id, title=d.title, positions=d.positions, status=d.status, location=d.location,
        start_date=d.start_date, sent_submissions=d.sent_submissions, url=f"/client/demands/{d.id}",
    )  # fmt: skip


async def _list_demands(tctx: Any, args: DemandsIn) -> ToolOutcome:
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        rows = await demands_portal.client_list_demands(act.ctx, db, args.status)
    out = DemandsOut(items=[_demand(d) for d in rows[:MAX]], total=len(rows))
    return ToolOutcome(
        guard(KIND, out), refs=tuple(("demand", d.id) for d in rows[:MAX]), summary=f"{len(rows)} demands"
    )


class DemandIdIn(In):
    demand_id: uuid.UUID


async def _get_demand(tctx: Any, args: DemandIdIn) -> ToolOutcome:
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        d = await demands_portal.client_get_demand(act.ctx, db, args.demand_id)
    return ToolOutcome(guard(KIND, _demand(d)), refs=(("demand", d.id),), summary="demand")


class SubmissionsIn(In):
    demand_id: uuid.UUID | None = None


class SubmissionOut(Out):
    id: uuid.UUID
    human_id: str
    demand_id: uuid.UUID
    demand_title: str | None
    candidate_display_name: str
    headline: str | None
    experience_months: int | None
    availability_days: int | None
    recruiter_summary: str | None
    feedback_status: str
    interview_status: str | None
    sent_at: datetime | None
    url: str


class SubmissionsOut(Out):
    items: list[SubmissionOut]
    total: int


def _submission(s: Any) -> SubmissionOut:
    return SubmissionOut(
        id=s.id, human_id=s.human_id, demand_id=s.demand_id, demand_title=s.demand_title,
        candidate_display_name=s.candidate_display_name, headline=s.headline, experience_months=s.experience_months,
        availability_days=s.availability_days, recruiter_summary=s.recruiter_summary, feedback_status=s.feedback_status,
        interview_status=s.interview_status, sent_at=s.sent_at, url=f"/client/demands/{s.demand_id}?submission={s.id}",
    )  # fmt: skip


async def _awaiting_feedback(tctx: Any, args: SubmissionsIn) -> ToolOutcome:
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        rows = await pipeline_portal.client_list_submissions(act.ctx, db, args.demand_id)
    rows = [s for s in rows if s.feedback_status == "none"]
    out = SubmissionsOut(items=[_submission(s) for s in rows[:MAX]], total=len(rows))
    return ToolOutcome(
        guard(KIND, out),
        refs=tuple(("submission", s.id) for s in rows[:MAX]),
        summary=f"{len(rows)} awaiting feedback",
    )


class SubmissionIdIn(In):
    submission_id: uuid.UUID


async def _get_submission(tctx: Any, args: SubmissionIdIn) -> ToolOutcome:
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        s = await pipeline_portal.client_get_submission(act.ctx, db, args.submission_id)
    return ToolOutcome(guard(KIND, _submission(s)), refs=(("submission", s.id),), summary="submission")


class EmptyIn(In):
    pass


class InterviewOut(Out):
    interview_id: uuid.UUID
    demand_title: str | None
    candidate_display_name: str
    starts_at: datetime


class InterviewsOut(Out):
    items: list[InterviewOut]
    total: int


async def _list_interviews(tctx: Any, args: EmptyIn) -> ToolOutcome:  # ruff: ignore[unused-function-argument]
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        rows = await interviews_portal.client_upcoming_interviews(act.ctx, db)
    items = [
        InterviewOut(
            interview_id=r.interview_id,
            demand_title=r.demand_title,
            candidate_display_name=r.candidate_display_name,
            starts_at=r.starts_at,
        )
        for r in rows[:MAX]
    ]
    return ToolOutcome(
        guard(KIND, InterviewsOut(items=items, total=len(rows))), summary=f"{len(rows)} interviews"
    )


class PlacementsIn(In):
    status: str | None = Field(default=None, max_length=20)


class PlacementOut(Out):
    id: uuid.UUID
    human_id: str
    worker_name: str
    role_title: str | None
    start_date: date | None
    end_date: date | None
    status: str
    ending_soon: bool
    timesheet_status: str | None
    url: str


class PlacementsOut(Out):
    items: list[PlacementOut]
    total: int


async def _list_placements(tctx: Any, args: PlacementsIn) -> ToolOutcome:
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        rows = await placements_portal.client_placements(act.ctx, db, args.status)
    items = [
        PlacementOut(
            id=p.id,
            human_id=p.human_id,
            worker_name=p.worker_name,
            role_title=p.role_title,
            start_date=p.start_date,
            end_date=p.end_date,
            status=p.status,
            ending_soon=p.ending_soon,
            timesheet_status=p.timesheet_status,
            url=f"/client/team/{p.id}",
        )
        for p in rows[:MAX]
    ]
    return ToolOutcome(
        guard(KIND, PlacementsOut(items=items, total=len(rows))),
        refs=tuple(("placement", p.id) for p in rows[:MAX]),
        summary=f"{len(rows)} placements",
    )


APPROVAL_PERMISSION = {
    "timesheet": "time:approve",
    "expense_claim": "time:approve",
    "demand": "demands:approve",
    "deliverable": "deliverables:accept",
}


class ApprovalOut(Out):
    kind: str
    title: str
    subtitle: str | None
    submitted_at: datetime | None
    url: str


class ApprovalsOut(Out):
    items: list[ApprovalOut]
    total: int
    guidance: str


async def _pending_approvals(tctx: Any, args: EmptyIn) -> ToolOutcome:  # ruff: ignore[unused-function-argument]
    act = await acting(tctx, KIND)
    items: list[ApprovalOut] = []
    if any(act.has(p) for p in ("time:approve", "demands:approve", "deliverables:accept")):
        async with session(tctx, act) as db:
            hub = await approvals_portal.hub(act.ctx, db)
        for group in hub.kinds:
            needed = APPROVAL_PERMISSION.get(group.kind)
            if needed is not None and not act.has(needed):
                continue
            for item in group.items:
                items.append(
                    ApprovalOut(
                        kind=group.label,
                        title=item.title,
                        subtitle=item.subtitle,
                        submitted_at=item.submitted_at,
                        url=item.link or "/client/approvals",
                    )
                )
    out = ApprovalsOut(
        items=items[:MAX],
        total=len(items),
        guidance="Approve or reject these yourself in Approvals; Copilot cannot decide them.",
    )
    return ToolOutcome(guard(KIND, out), summary=f"{len(items)} pending approvals")


class InvoiceOut(Out):
    id: uuid.UUID
    kind: str
    number: str | None
    doc_date: date | None
    due_date: date | None
    currency: str
    document_total: str
    status: str
    payment_status: str | None
    url: str


class InvoicesOut(Out):
    items: list[InvoiceOut]
    total: int


async def _list_invoices(tctx: Any, args: EmptyIn) -> ToolOutcome:  # ruff: ignore[unused-function-argument]
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        rows = await billing_portal.client_list(act.ctx, db)
    items = [
        InvoiceOut(
            id=i.id,
            kind=i.kind,
            number=i.number,
            doc_date=i.doc_date,
            due_date=i.due_date,
            currency=i.currency,
            document_total=str(i.document_total),
            status=i.status,
            payment_status=i.payment_status,
            url=f"/client/invoices/{i.kind}/{i.id}",
        )
        for i in rows[:MAX]
    ]
    return ToolOutcome(
        guard(KIND, InvoicesOut(items=items, total=len(rows))), summary=f"{len(rows)} invoices"
    )


class ScreenIn(In):
    intent: Literal[tuple(SCREENS)]  # type: ignore[valid-type]


class ScreenOut(Out):
    label: str
    url: str
    reason: str


async def _screen_for(tctx: Any, args: ScreenIn) -> ToolOutcome:
    await acting(tctx, KIND)
    label, url, reason = SCREENS[args.intent]
    return ToolOutcome(ScreenOut(label=label, url=url, reason=reason), summary=f"screen {url}")


# ================================================================ drafts
class DemandRequestIn(In):
    title: str = Field(min_length=2, max_length=300)
    positions: int = Field(default=1, ge=1, le=500)
    skills: list[str] = Field(default_factory=list, max_length=30)
    location: str | None = Field(default=None, max_length=300)
    start_date: date | None = None
    description: str | None = Field(default=None, max_length=4000)


class DemandRequestOut(Out):
    id: uuid.UUID
    req_id: str
    title: str
    status: str
    url: str


async def _demand_request_preview(tctx: Any, args: DemandRequestIn) -> ProposalPreview:
    await acting(tctx, KIND)
    return ProposalPreview(
        title="Draft: new requirement request",
        summary=f"Request a new requirement: {args.title} ({args.positions} position(s)).",
        fields=(("Title", args.title), ("Positions", str(args.positions)), ("Location", args.location or "—"),
                ("Start date", args.start_date.isoformat() if args.start_date else "—"), ("Skills", ", ".join(args.skills) or "—")),
    )  # fmt: skip


async def _demand_request(tctx: Any, args: DemandRequestIn) -> ToolOutcome:
    from app.schemas.demands_portal import ClientDemandRequestIn

    act = await acting(tctx, KIND)
    data = ClientDemandRequestIn(
        title=args.title, positions=args.positions, skills=args.skills, location=args.location, start_date=args.start_date,
        description=args.description,
    )  # fmt: skip
    out = await demands_portal.client_create_request(act.ctx, data, None)
    return ToolOutcome(
        DemandRequestOut(id=out.id, req_id=out.req_id, title=out.title, status=out.status, url=f"/client/demands/{out.id}"),
        target=("demand", out.id), summary="demand request created",
    )  # fmt: skip


class FeedbackDraftIn(In):
    submission_id: uuid.UUID
    decision: Literal["interested", "on_hold"]
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackDraftOut(Out):
    submission_id: uuid.UUID
    feedback_status: str
    url: str


async def _feedback_preview(tctx: Any, args: FeedbackDraftIn) -> ProposalPreview:
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        s = await pipeline_portal.client_get_submission(
            act.ctx, db, args.submission_id
        )  # NotFound outside scope
    label = "Interested" if args.decision == "interested" else "On hold"
    return ProposalPreview(
        title=f"Draft: feedback for {s.candidate_display_name}",
        summary=f"{label}. {args.comment or ''}".strip(),
        fields=(("Candidate", s.candidate_display_name), ("Requirement", s.demand_title or "—"), ("Decision", label), ("Comment", args.comment or "—")),
    )  # fmt: skip


async def _feedback(tctx: Any, args: FeedbackDraftIn) -> ToolOutcome:
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        s = await pipeline_portal.client_feedback(
            act.ctx, db, args.submission_id, args.decision, args.comment
        )
    return ToolOutcome(
        FeedbackDraftOut(submission_id=s.id, feedback_status=s.feedback_status, url=f"/client/demands/{s.demand_id}?submission={s.id}"),
        target=("submission", s.id), summary="feedback recorded",
    )  # fmt: skip


class MessageDraftIn(In):
    text: str = Field(min_length=2, max_length=1000)


class MessageDraftOut(Out):
    sent: bool
    reference: uuid.UUID


async def _message_preview(tctx: Any, args: MessageDraftIn) -> ProposalPreview:
    await acting(tctx, KIND)
    return ProposalPreview(
        title="Draft: message to your account manager", summary=args.text, fields=(("Message", args.text),)
    )


async def _message(tctx: Any, args: MessageDraftIn) -> ToolOutcome:
    act = await acting(tctx, KIND)
    task_id = await handoff.message_account_manager(act.tenant_id, act.org_id, args.text)
    return ToolOutcome(
        MessageDraftOut(sent=True, reference=task_id), summary="message sent to the account manager"
    )


# ================================================================ registration
def _spec(
    key: str,
    title: str,
    description: str,
    inp: Any,
    out: Any,
    perms: tuple[str, ...],
    handler: Any,
    **kw: Any,
) -> ToolSpec:
    return ToolSpec(
        key=f"portal_client.{key}", version=1, owner_epic=OWNER_EPIC, title=title, description=description, input_model=inp,
        output_model=out, permissions=(USE, *perms), risk=kw.pop("risk", "read"), scope=kw.pop("scope", NONE), handler=handler, **kw,
    )  # fmt: skip


TOOLS: tuple[ToolSpec, ...] = (
    _spec(
        "list_demands",
        "List my requirements",
        "List the requirements (demands) of your organisation that you can see, newest first, with status and how many candidates were sent.",
        DemandsIn,
        DemandsOut,
        (),
        _list_demands,
    ),
    _spec(
        "get_demand",
        "Get a requirement",
        "Get one requirement you can see by id.",
        DemandIdIn,
        DemandOut,
        (),
        _get_demand,
    ),
    _spec(
        "awaiting_feedback",
        "Candidates awaiting my feedback",
        "List candidates sent to you that still need your feedback, optionally for one requirement. Candidate summaries are text written by the recruiter.",
        SubmissionsIn,
        SubmissionsOut,
        ("submissions:review",),
        _awaiting_feedback,
        untrusted_output=frozenset({"items.recruiter_summary", "items.headline"}),
    ),
    _spec(
        "get_submission",
        "Get a submitted candidate",
        "Get one submitted candidate you can see by submission id.",
        SubmissionIdIn,
        SubmissionOut,
        ("submissions:review",),
        _get_submission,
        untrusted_output=frozenset({"recruiter_summary", "headline"}),
    ),
    _spec(
        "list_interviews",
        "My upcoming interviews",
        "List upcoming interviews with candidates sent to you.",
        EmptyIn,
        InterviewsOut,
        ("submissions:review",),
        _list_interviews,
    ),
    _spec(
        "list_placements",
        "My team",
        "List the workers placed with your organisation that you can see.",
        PlacementsIn,
        PlacementsOut,
        ("placements:view",),
        _list_placements,
    ),
    _spec(
        "pending_approvals",
        "My pending approvals",
        "List what is waiting for your approval (timesheets, expense claims, requirements, deliverables) with links. Copilot never approves or rejects; you decide in Approvals.",
        EmptyIn,
        ApprovalsOut,
        (),
        _pending_approvals,
    ),
    _spec(
        "list_invoices",
        "My invoices",
        "List your organisation's invoices, credit and debit notes with payment status.",
        EmptyIn,
        InvoicesOut,
        ("invoices:view",),
        _list_invoices,
    ),
    _spec(
        "screen_for",
        "Where to do this",
        "Return the portal screen where you do something Copilot cannot do for you (approve, reject, accept, give interview feedback) or where information lives.",
        ScreenIn,
        ScreenOut,
        (),
        _screen_for,
    ),
    _spec(
        "draft_demand_request",
        "Request a new requirement",
        "Prepare a request for a new requirement for you to review; nothing is sent until you confirm it.",
        DemandRequestIn,
        DemandRequestOut,
        (),
        _demand_request,
        risk="low",
        preview=_demand_request_preview,
        untrusted_input=frozenset({"description"}),
    ),
    _spec(
        "draft_submission_feedback",
        "Give feedback on a candidate",
        "Prepare feedback (interested or on hold, with a comment) on a candidate sent to you, for you to review and confirm. Rejecting a candidate is done by you on the submission screen.",
        FeedbackDraftIn,
        FeedbackDraftOut,
        ("submissions:review",),
        _feedback,
        risk="low",
        preview=_feedback_preview,
        scope=ScopeRule(resource="submission", target_arg="submission_id"),
    ),
    _spec(
        "draft_message",
        "Message your account manager",
        "Prepare a message to your account manager for you to review; it is delivered only when you confirm it.",
        MessageDraftIn,
        MessageDraftOut,
        (),
        _message,
        risk="low",
        preview=_message_preview,
    ),
)

for _tool in TOOLS:
    if _tool.key not in _registered():
        register_tool(_tool)

__all__ = ["SCREENS", "TOOLS"]
