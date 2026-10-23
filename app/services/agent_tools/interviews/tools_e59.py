# ruff: file-ignore[line-too-long]
"""Scheduling Agent tools of the ``interviews`` domain (Epic 59 Part B; ``owner_epic="59"``, ``version=1``).

| Key | Risk | Default after burn-in |
|---|---|---|
| ``interviews.plan`` / ``interviews.participants`` | read | auto |
| ``interviews.book`` / ``interviews.reschedule`` | low; ``high`` with ``accept_conflicts`` (``risk_fn``) | auto |
| ``interviews.cancel`` / ``interviews.change_participants`` | high | Proposal only |

Booking is low risk only for the slot the Candidate picked from the current offer (or a Candidate-proposed time that
matched) in the Application's interview plan, with the offer's participants, every connected participant free at
re-check and inside working hours. Anything else is a ``slot_conflict`` result or, with ``accept_conflicts``, a Proposal.
★ (absent): whether a round happens, outcomes, no-show stage moves, Offers.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.core.agentic import IdempotencyRule, ProposalPreview, ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.core.errors import NotFound
from app.services.agent_tools.scheduling.common_e59 import (
    OWNER_EPIC,
    VERSION,
    In,
    Out,
    load_request,
    refs_of,
    subject_application,
    svc,
)

REQ_SCOPE_KW: dict[str, Any] = {
    "resource": "scheduling_request",
    "target_arg": "request_id",
    "cross_subject": True,
}


def _preview(title: str, summary: Any) -> Any:
    async def preview(tctx: Any, args: Any) -> ProposalPreview:
        # A Proposal is only created for a record of the run's Application (else not_found, no Proposal).
        if getattr(args, "request_id", None) is not None:
            await load_request(tctx, args.request_id)
        if getattr(args, "interview_id", None) is not None:
            await svc.booking.interview_for_tool(
                tctx.db, args.interview_id, subject_application(tctx) or uuid.UUID(int=0)
            )
        fields = tuple(
            (k, str(v)) for k, v in args.model_dump(mode="json").items() if v not in (None, [], "")
        )
        return ProposalPreview(title=title, summary=str(summary(args))[:4000], fields=fields[:12])

    return preview


# ================================================================ interviews.plan (read)
class PlanIn(In):
    application_id: uuid.UUID
    stage_id: uuid.UUID | None = None


class PlanStage(Out):
    id: uuid.UUID
    name: str
    kind: str
    duration: int
    is_final: bool


class PlanOut(Out):
    stages: list[PlanStage]
    current_stage_id: uuid.UUID | None


async def _plan(tctx: Any, args: PlanIn) -> ToolOutcome:
    from app.services import interviews_service
    from app.services.scheduling_agent.common import as_user

    if subject_application(tctx) != args.application_id:
        raise NotFound("Application not found")
    app = await svc.requests.load_application(tctx.request, tctx.db, args.application_id)
    plan = await interviews_service.get_plan(as_user(tctx.request), tctx.db, app.demand_id)
    stages = [
        PlanStage(
            id=s.id, name=s.name, kind=str(s.kind), duration=s.default_duration_minutes, is_final=s.is_final
        )
        for s in plan.stages
    ]
    current = args.stage_id if args.stage_id in {s.id for s in stages} else None
    return ToolOutcome(
        PlanOut(stages=stages, current_stage_id=current),
        refs=(("application", app.id),),
        candidate_ids=(app.candidate_id,),
        summary=f"{len(stages)} stage(s)",
    )


register_tool(
    ToolSpec(
        key="interviews.plan", version=VERSION, owner_epic=OWNER_EPIC, title="Read the interview plan",
        description="The approved interview plan of the application's demand: stages with kind, default duration and whether final.",
        input_model=PlanIn, output_model=PlanOut, permissions=("interview:read",), risk="read",
        scope=ScopeRule(resource="application", target_arg="application_id"), handler=_plan,
    )
)  # fmt: skip


# ================================================================ interviews.participants (read)
class ParticipantsIn(In):
    request_id: uuid.UUID


class InternalParticipant(Out):
    user_id: uuid.UUID
    name: str | None
    timezone: str
    calendar_connected: bool
    working_hours: dict[str, Any]


class ParticipantsOut(Out):
    internal: list[InternalParticipant]
    external_count: int


async def _participants(tctx: Any, args: ParticipantsIn) -> ToolOutcome:
    req = await load_request(tctx, args.request_id)
    data = await svc.settings_api.participants_view(tctx.db, req)
    out = ParticipantsOut(
        internal=[InternalParticipant(**p) for p in data["internal"]], external_count=data["external_count"]
    )
    return ToolOutcome(
        out,
        refs=refs_of(req),
        candidate_ids=(req.candidate_id,),
        summary=f"{len(out.internal)} interviewer(s)",
    )


register_tool(
    ToolSpec(
        key="interviews.participants", version=VERSION, owner_epic=OWNER_EPIC, title="Read the interviewers",
        description="The request's internal interviewers (name, time zone, whether a calendar is connected, working hours) and the number of external interviewers. No emails.",
        input_model=ParticipantsIn, output_model=ParticipantsOut, permissions=("interview:read",), risk="read",
        scope=ScopeRule(**REQ_SCOPE_KW), handler=_participants,
    )
)  # fmt: skip


# ================================================================ interviews.book / interviews.reschedule
class WindowIn(In):
    start: datetime
    end: datetime


class BookIn(In):
    request_id: uuid.UUID
    offer_id: uuid.UUID | None = None
    pick_id: uuid.UUID | None = None
    window: WindowIn | None = None
    accept_conflicts: bool = False


class BookOut(Out):
    status: str
    interview_id: uuid.UUID | None = None
    starts_at: datetime | None = None
    sequence: int | None = None
    busy: list[str] = Field(default_factory=list)
    outside_working_hours: bool = False
    error: str | None = None


def _risk(args: Any) -> str:
    return "high" if getattr(args, "accept_conflicts", False) else "low"


def _request_version(probe: Any) -> Any:
    async def version() -> str:
        from app.models.scheduling_agent import SchedulingRequest

        req = await probe.db.get(SchedulingRequest, probe.args.request_id)
        return f"{req.status}:{req.round}" if req is not None else "missing"

    return version()


async def _resolve_pick(tctx: Any, req: Any, args: BookIn) -> uuid.UUID | None:
    if args.pick_id is not None or args.offer_id is None:
        return args.pick_id
    from app.models.scheduling_agent import SchedulingSlotOffer

    offer = await tctx.db.get(SchedulingSlotOffer, args.offer_id)
    if offer is None or offer.request_id != req.id:
        raise NotFound("Offer not found")
    return offer.pick_id


async def _do_book(tctx: Any, args: BookIn, *, reschedule: bool) -> ToolOutcome:
    req = await load_request(tctx, args.request_id, for_update=True)
    if reschedule and req.kind != "reschedule":
        from app.core.errors import ConflictError

        raise ConflictError("Use interviews.book for a new interview", code="not_a_reschedule")
    pick_id = await _resolve_pick(tctx, req, args)
    window = (args.window.start, args.window.end) if args.window is not None else None
    fn = svc.booking.reschedule if reschedule else svc.booking.book
    data = await fn(
        tctx.request, tctx.db, req.id, pick_id=pick_id, window=window, accept_conflicts=args.accept_conflicts
    )
    out = BookOut(
        status=data["status"], interview_id=data.get("interview_id"), starts_at=data.get("starts_at"), sequence=data.get("sequence"),
        busy=list(data.get("busy") or []), outside_working_hours=bool(data.get("outside_working_hours")), error=data.get("error"),
    )  # fmt: skip
    extra = (("interview", out.interview_id),) if out.interview_id else ()
    return ToolOutcome(
        out,
        refs=refs_of(req, *extra),
        candidate_ids=(req.candidate_id,),
        target=("scheduling_request", req.id),
        summary=f"booking: {out.status}",
    )


async def _book(tctx: Any, args: BookIn) -> ToolOutcome:
    return await _do_book(tctx, args, reschedule=False)


async def _reschedule(tctx: Any, args: BookIn) -> ToolOutcome:
    return await _do_book(tctx, args, reschedule=True)


register_tool(
    ToolSpec(
        key="interviews.book", version=VERSION, owner_epic=OWNER_EPIC, title="Booked the interview",
        description=(
            "Book the interview for the slot the candidate picked (offer_id / pick_id) or a candidate-proposed window that "
            "scheduling.check_times matched (window). Re-checks free/busy first: status 'slot_conflict' means the time is no "
            "longer free (offer new times); 'deduplicated' means it was already booked. Books through the interview scheduler "
            "(invites, meeting link). accept_conflicts=true books despite conflicts and always needs a person's approval."
        ),
        input_model=BookIn, output_model=BookOut, permissions=("interview:schedule", "calendar:free_busy"), risk="low",
        risk_fn=_risk,  # type: ignore[arg-type]
        scope=ScopeRule(**REQ_SCOPE_KW, version=_request_version), handler=_book,
        preview=_preview("Book the interview", lambda a: ("Book despite conflicts" if a.accept_conflicts else "Book the picked time") + f" for request {a.request_id}"),
        idempotency=IdempotencyRule(fields=("pick_id", "offer_id", "window"), per_run=False), default_mode="auto", max_calls_per_run=4,
    )
)  # fmt: skip

register_tool(
    ToolSpec(
        key="interviews.reschedule", mcp_exposed=True, version=VERSION, owner_epic=OWNER_EPIC, title="Rescheduled the interview",
        description=(
            "For a reschedule request only: move the booked interview to the slot the candidate picked (or a matched window). "
            "The invitation keeps its identity and its sequence is bumped. Same re-check and conflict rules as interviews.book."
        ),
        input_model=BookIn, output_model=BookOut, permissions=("interview:schedule",), risk="low", risk_fn=_risk,  # type: ignore[arg-type]
        scope=ScopeRule(**REQ_SCOPE_KW, version=_request_version), handler=_reschedule,
        preview=_preview("Reschedule the interview", lambda a: ("Reschedule despite conflicts" if a.accept_conflicts else "Move to the picked time") + f" for request {a.request_id}"),
        idempotency=IdempotencyRule(fields=("pick_id", "offer_id", "window"), per_run=False), default_mode="auto", max_calls_per_run=4,
    )
)  # fmt: skip


# ================================================================ interviews.cancel / change_participants (high)
def _interview_version(probe: Any) -> Any:
    async def version() -> str:
        from app.models.interviews import Interview

        row = await probe.db.get(Interview, probe.args.interview_id)
        return str(row.version) if row is not None else "missing"

    return version()


class CancelIn(In):
    interview_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=500)


class CancelOut(Out):
    status: str


async def _cancel(tctx: Any, args: CancelIn) -> ToolOutcome:
    row = await svc.booking.interview_for_tool(tctx.db, args.interview_id, subject_application(tctx))
    data = await svc.booking.cancel_interview(tctx.request, tctx.db, row.id, args.reason)
    return ToolOutcome(
        CancelOut(status=data["status"]),
        refs=(("interview", row.id),),
        candidate_ids=(row.candidate_id,),
        target=("interview", row.id),
        summary="interview cancelled",
    )


register_tool(
    ToolSpec(
        key="interviews.cancel", mcp_exposed=True, version=VERSION, owner_epic=OWNER_EPIC, title="Cancel the interview",
        description="Propose cancelling a scheduled interview of this application (everyone gets a cancellation). Always a Proposal: a person decides.",
        input_model=CancelIn, output_model=CancelOut, permissions=("interview:schedule",), risk="high",
        scope=ScopeRule(resource="interview", target_arg="interview_id", cross_subject=True, version=_interview_version), handler=_cancel,
        preview=_preview("Cancel the interview", lambda a: f"Cancel interview {a.interview_id}: {a.reason}"), default_mode="suggest", max_calls_per_run=2,
    )
)  # fmt: skip


class ChangeParticipantsIn(In):
    interview_id: uuid.UUID
    add_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5)
    remove_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5)
    reason: str = Field(min_length=1, max_length=500)


class ChangeParticipantsOut(Out):
    interview_id: uuid.UUID
    sequence: int


async def _change(tctx: Any, args: ChangeParticipantsIn) -> ToolOutcome:
    row = await svc.booking.interview_for_tool(tctx.db, args.interview_id, subject_application(tctx))
    data = await svc.booking.change_participants(
        tctx.request,
        tctx.db,
        row.id,
        add_user_ids=list(args.add_user_ids),
        remove_user_ids=list(args.remove_user_ids),
    )
    return ToolOutcome(
        ChangeParticipantsOut(**data),
        refs=(("interview", row.id),),
        candidate_ids=(row.candidate_id,),
        target=("interview", row.id),
        summary="interviewers changed",
    )


register_tool(
    ToolSpec(
        key="interviews.change_participants", version=VERSION, owner_epic=OWNER_EPIC, title="Change the interviewers",
        description="Propose adding (up to 5) or removing (up to 5) internal interviewers of a scheduled interview at the same time. Always a Proposal.",
        input_model=ChangeParticipantsIn, output_model=ChangeParticipantsOut, permissions=("interview:schedule",), risk="high",
        scope=ScopeRule(resource="interview", target_arg="interview_id", cross_subject=True, version=_interview_version), handler=_change,
        preview=_preview("Change the interviewers", lambda a: f"Add {len(a.add_user_ids)} / remove {len(a.remove_user_ids)} interviewer(s): {a.reason}"),
        default_mode="suggest", max_calls_per_run=2,
    )
)  # fmt: skip
