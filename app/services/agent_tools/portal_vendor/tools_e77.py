# ruff: file-ignore[line-too-long]
"""Epic 77 Vendor Portal Copilot tools (``portal_vendor.*``, FR-398). Each wraps ``vendors.portal`` acting as the
Vendor User (org RLS ``vendor``: only the Vendor's own releases, submissions, placements and statements; another
Vendor's ids are not found). Needs ``portal_copilot:use`` plus the portal permission of the wrapped endpoint.

``portal_vendor.draft_candidate_profile`` prepares a prefilled profile form for a Release; confirming it returns the
Submission screen link (``/vendor/releases/{id}/submit?draft=<id>``) and stores nothing in vendor data -- the Vendor
User completes and submits the candidate themselves (submitting is never a Copilot action)."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field

from app.core.agentic import ProposalPreview, ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.core.agentic.tools import tools as _registered
from app.services.agent_tools.portal_common import handoff
from app.services.agent_tools.portal_common.tooling import OWNER_EPIC, In, Out, acting, guard, session
from app.services.vendors import portal as vendor_portal

KIND = "vendor"
USE = "portal_copilot:use"
MAX = 20
NONE = ScopeRule(resource="none")
ALLOW = frozenset({"client_name"})  # revealed client names only (ClientNameRevealed, FR-80)

SCREENS: dict[str, tuple[str, str, str]] = {
    "submit_candidate": (
        "Open Requirements",
        "/vendor/releases",
        "You submit candidates yourself on the requirement's Submit screen.",
    ),
    "withdraw_submission": (
        "Open Submissions",
        "/vendor/submissions",
        "You can withdraw a submission on the Submissions screen.",
    ),
    "upload_document": (
        "Open Documents",
        "/vendor/documents",
        "Upload your documents on the Documents screen.",
    ),
    "statements": ("Open Statements", "/vendor/statements", "Your payable statements are in Statements."),
    "scorecard": ("Open Scorecard", "/vendor/scorecard", "Your performance scorecard is in Scorecard."),
    "users": ("Open Users", "/vendor/users", "Vendor administrators manage users on the Users screen."),
    "other": ("Open the portal", "/vendor", "I can't do this for you; you can do it yourself in the portal."),
}


class EmptyIn(In):
    pass


class ReleaseOut(Out):
    id: uuid.UUID
    human_id: str
    status: str
    title: str
    positions: int
    skills: list[str]
    location: str | None
    experience_range: str | None
    client_name: str
    remaining_slots: int | None
    deadline_at: datetime | None
    jd_public_text: str | None
    url: str


class ReleasesOut(Out):
    items: list[ReleaseOut]
    total: int


def _release(r: Any) -> ReleaseOut:
    return ReleaseOut(
        id=r.id, human_id=r.human_id, status=r.status, title=r.title, positions=r.positions, skills=list(r.skills),
        location=r.location, experience_range=r.experience_range, client_name=r.client_name, remaining_slots=r.remaining_slots,
        deadline_at=r.deadline_at, jd_public_text=(r.jd_public_text or None) and r.jd_public_text[:2000], url=f"/vendor/releases/{r.id}",
    )  # fmt: skip


async def _list_releases(tctx: Any, args: EmptyIn) -> ToolOutcome:  # ruff: ignore[unused-function-argument]
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        rows = await vendor_portal.list_releases(act.ctx, db, act.principal)
    rows = [r for r in rows if r.status == "active"]
    out = ReleasesOut(items=[_release(r) for r in rows[:MAX]], total=len(rows))
    return ToolOutcome(guard(KIND, out, allow=ALLOW), summary=f"{len(rows)} open releases")


class ReleaseIdIn(In):
    release_id: uuid.UUID


async def _get_release(tctx: Any, args: ReleaseIdIn) -> ToolOutcome:
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        r = await vendor_portal.get_release(act.ctx, db, act.principal, args.release_id)
    return ToolOutcome(guard(KIND, _release(r), allow=ALLOW), summary="release")


class SubmissionsIn(In):
    release_id: uuid.UUID | None = None
    pending_only: bool = False


class SubmissionOut(Out):
    id: uuid.UUID
    human_id: str
    release_id: uuid.UUID
    release_title: str | None
    full_name: str | None
    status: str
    label: str
    next_interview_at: datetime | None
    placed: bool
    created_at: datetime
    url: str


class SubmissionsOut(Out):
    items: list[SubmissionOut]
    total: int


PENDING_LABELS = frozenset({"Submitted", "Under review", "Pending", "Received", "In review"})


def _submission(s: Any) -> SubmissionOut:
    return SubmissionOut(
        id=s.id, human_id=s.human_id, release_id=s.release_id, release_title=s.release_title, full_name=s.full_name,
        status=s.status, label=s.label, next_interview_at=s.next_interview_at, placed=s.placed, created_at=s.created_at,
        url=f"/vendor/submissions/{s.id}",
    )  # fmt: skip


async def _list_submissions(tctx: Any, args: SubmissionsIn) -> ToolOutcome:
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        rows = await vendor_portal.list_submissions(act.ctx, db, act.principal, release_id=args.release_id)
    if args.pending_only:
        rows = [s for s in rows if not s.placed and s.outcome_category is None]
    out = SubmissionsOut(items=[_submission(s) for s in rows[:MAX]], total=len(rows))
    return ToolOutcome(guard(KIND, out), summary=f"{len(rows)} submissions")


class SubmissionIdIn(In):
    submission_id: uuid.UUID


async def _get_submission(tctx: Any, args: SubmissionIdIn) -> ToolOutcome:
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        s = await vendor_portal.get_submission(act.ctx, db, act.principal, args.submission_id)
    return ToolOutcome(guard(KIND, _submission(s)), summary="submission")


class PlacementOut(Out):
    id: uuid.UUID
    human_id: str
    worker_name: str | None
    role_title: str | None
    start_date: date | None
    end_date: date | None
    status: str


class PlacementsOut(Out):
    items: list[PlacementOut]
    total: int


async def _list_placements(tctx: Any, args: EmptyIn) -> ToolOutcome:  # ruff: ignore[unused-function-argument]
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        rows = await vendor_portal.list_placements(act.ctx, db, act.principal)
    items = [PlacementOut(**p.model_dump(include=set(PlacementOut.model_fields))) for p in rows[:MAX]]
    return ToolOutcome(
        guard(KIND, PlacementsOut(items=items, total=len(rows))), summary=f"{len(rows)} placements"
    )


class StatementOut(Out):
    id: uuid.UUID
    human_id: str
    period_start: date
    period_end: date
    status: str
    currency: str
    total_amount: str
    url: str


class StatementsOut(Out):
    items: list[StatementOut]
    total: int


async def _list_statements(tctx: Any, args: EmptyIn) -> ToolOutcome:  # ruff: ignore[unused-function-argument]
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        rows = await vendor_portal.list_statements(act.ctx, db, act.principal)
    items = [
        StatementOut(
            id=s.id,
            human_id=s.human_id,
            period_start=s.period_start,
            period_end=s.period_end,
            status=s.status,
            currency=s.currency,
            total_amount=str(s.total_amount),
            url="/vendor/statements",
        )
        for s in rows[:MAX]
    ]
    return ToolOutcome(
        guard(KIND, StatementsOut(items=items, total=len(rows))), summary=f"{len(rows)} statements"
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
class ProfileDraftIn(In):
    release_id: uuid.UUID
    full_name: str = Field(min_length=1, max_length=200)
    skills: list[str] = Field(default_factory=list, max_length=30)
    total_experience_months: int | None = Field(default=None, ge=0, le=900)
    current_location: str | None = Field(default=None, max_length=200)
    availability_date: date | None = None
    notice_period_days: int | None = Field(default=None, ge=0, le=365)


class ProfileDraftOut(Out):
    open_url: str
    release_id: uuid.UUID


async def _profile_preview(tctx: Any, args: ProfileDraftIn) -> ProposalPreview:
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        r = await vendor_portal.get_release(
            act.ctx, db, act.principal, args.release_id
        )  # NotFound for other vendors
    return ProposalPreview(
        title=f"Draft: candidate profile for {r.title}"[:200],
        summary=f"A prefilled profile for {args.full_name}. You complete and submit it on the Submit screen.",
        fields=(("Name", args.full_name), ("Skills", ", ".join(args.skills) or "—"),
                ("Experience (months)", str(args.total_experience_months) if args.total_experience_months is not None else "—"),
                ("Location", args.current_location or "—"),
                ("Available from", args.availability_date.isoformat() if args.availability_date else "—")),
    )  # fmt: skip


async def _profile(tctx: Any, args: ProfileDraftIn) -> ToolOutcome:
    act = await acting(tctx, KIND)
    async with session(tctx, act) as db:
        await vendor_portal.get_release(act.ctx, db, act.principal, args.release_id)
    proposal_id = tctx.request.proposal_id
    url = f"/vendor/releases/{args.release_id}/submit" + (f"?draft={proposal_id}" if proposal_id else "")
    return ToolOutcome(
        ProfileDraftOut(open_url=url, release_id=args.release_id), summary="profile form prepared"
    )


class MessageDraftIn(In):
    text: str = Field(min_length=2, max_length=1000)


class MessageDraftOut(Out):
    sent: bool
    reference: uuid.UUID


async def _message_preview(tctx: Any, args: MessageDraftIn) -> ProposalPreview:
    await acting(tctx, KIND)
    return ProposalPreview(
        title="Draft: message to your vendor manager", summary=args.text, fields=(("Message", args.text),)
    )


async def _message(tctx: Any, args: MessageDraftIn) -> ToolOutcome:
    act = await acting(tctx, KIND)
    task_id = await handoff.message_vendor_manager(act.tenant_id, act.org_id, args.text)
    return ToolOutcome(
        MessageDraftOut(sent=True, reference=task_id), summary="message sent to the vendor manager"
    )


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
        key=f"portal_vendor.{key}", version=1, owner_epic=OWNER_EPIC, title=title, description=description, input_model=inp,
        output_model=out, permissions=(USE, *perms), risk=kw.pop("risk", "read"), scope=kw.pop("scope", NONE), handler=handler, **kw,
    )  # fmt: skip


TOOLS: tuple[ToolSpec, ...] = (
    _spec(
        "list_releases",
        "Open requirements for us",
        "List the requirements (releases) open to your organisation. Job descriptions are text from the requirement.",
        EmptyIn,
        ReleasesOut,
        (),
        _list_releases,
        untrusted_output=frozenset({"items.jd_public_text"}),
    ),
    _spec(
        "get_release",
        "Get a requirement",
        "Get one requirement released to your organisation by id.",
        ReleaseIdIn,
        ReleaseOut,
        (),
        _get_release,
        untrusted_output=frozenset({"jd_public_text"}),
    ),
    _spec(
        "list_submissions",
        "Our submissions",
        "List your organisation's submissions and their status, optionally for one requirement or only those still pending.",
        SubmissionsIn,
        SubmissionsOut,
        (),
        _list_submissions,
    ),
    _spec(
        "get_submission",
        "Get a submission",
        "Get one of your organisation's submissions by id.",
        SubmissionIdIn,
        SubmissionOut,
        (),
        _get_submission,
    ),
    _spec(
        "list_placements",
        "Our placements",
        "List the placements of your organisation's workers.",
        EmptyIn,
        PlacementsOut,
        ("placements:view",),
        _list_placements,
    ),
    _spec(
        "list_statements",
        "Our statements",
        "List your organisation's finalized payable statements.",
        EmptyIn,
        StatementsOut,
        ("statements:view",),
        _list_statements,
    ),
    _spec(
        "screen_for",
        "Where to do this",
        "Return the portal screen where you do something Copilot cannot do for you (submit or withdraw a candidate, upload documents) or where information lives.",
        ScreenIn,
        ScreenOut,
        (),
        _screen_for,
    ),
    _spec(
        "draft_candidate_profile",
        "Prepare a candidate profile",
        "Prepare a prefilled candidate profile for a requirement from your description. You complete and submit it yourself on the Submit screen; Copilot never submits.",
        ProfileDraftIn,
        ProfileDraftOut,
        ("submissions:create",),
        _profile,
        risk="low",
        preview=_profile_preview,
        scope=ScopeRule(resource="vendor_release", target_arg="release_id"),
    ),
    _spec(
        "draft_message",
        "Message your vendor manager",
        "Prepare a question to your vendor manager for you to review; it is delivered only when you confirm it.",
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
