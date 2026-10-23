# ruff: file-ignore[line-too-long]
"""Epic 54 Copilot-owned draft tools (FR-278). ``risk="low"``; in a Copilot run every non-read tool is a Draft Action
(policy ``copilot_draft``) the user confirms with one click, executed as that user.

* ``copilot.propose_slots`` -> ``scheduling_agent_service.propose`` (stub until Epic 59). It sends nothing.
* ``copilot.prepare_pack`` -> ``submission_packs_service.prepare_pack`` (stub until Epic 61). It never sends.

Until the owners fill the stubs, confirming fails ``execution_failed`` / ``not_available`` (the card says "This isn't
available yet in your workspace."); 59 and 61 switch them on without a Copilot change.
"""

import uuid
from typing import Any

from pydantic import Field

from app.core.agentic import ProposalPreview, ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.core.errors import ConflictError
from app.services.agent_tools.copilot.common_e54 import OWNER_EPIC, In, Out, require_readable

NOT_AVAILABLE = "This isn't available yet in your workspace."


# ================================================================ copilot.propose_slots
class ProposeSlotsIn(In):
    application_id: uuid.UUID
    duration_minutes: int = Field(ge=15, le=240)
    participant_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=6)
    candidate_timezone: str = Field(default="Asia/Kolkata", min_length=3, max_length=60)
    max_slots: int = Field(default=5, ge=1, le=5)


class SlotOut(Out):
    start: str
    end: str
    all_known_free: bool


class ProposeSlotsOut(Out):
    proposal_ref: uuid.UUID
    slots: list[SlotOut]
    partial: bool


async def _propose_slots(tctx: Any, args: ProposeSlotsIn) -> ToolOutcome:
    from app.schemas.scheduling_agent import SlotProposalIn
    from app.services import scheduling_agent_service

    await require_readable(tctx, "application", args.application_id)
    try:
        out = await scheduling_agent_service.propose(
            tctx.request, tctx.db,
            SlotProposalIn(
                application_id=args.application_id, duration_minutes=args.duration_minutes,
                participant_user_ids=tuple(args.participant_user_ids), candidate_timezone=args.candidate_timezone,
                max_slots=args.max_slots,
            ),
        )  # fmt: skip
    except NotImplementedError as exc:
        raise ConflictError(NOT_AVAILABLE, code="not_available") from exc
    data = ProposeSlotsOut(
        proposal_ref=out.proposal_ref,
        slots=[
            SlotOut(start=s.start.isoformat(), end=s.end.isoformat(), all_known_free=s.all_known_free)
            for s in out.slots
        ],
        partial=out.partial,
    )
    return ToolOutcome(
        data, target=("application", args.application_id), summary=f"{len(data.slots)} slot options prepared"
    )


async def _slots_preview(tctx: Any, args: ProposeSlotsIn) -> ProposalPreview:  # ruff: ignore[unused-function-argument]
    return ProposalPreview(
        title="Propose interview slots",
        summary=(
            f"Prepare up to {args.max_slots} interview slot options of {args.duration_minutes} minutes "
            f"({args.candidate_timezone}). Nothing is sent to the candidate."
        ),
        fields=(
            ("Duration (minutes)", str(args.duration_minutes)),
            ("Interviewers", str(len(args.participant_user_ids))),
            ("Candidate time zone", args.candidate_timezone),
            ("Options", str(args.max_slots)),
        ),
        renderer="copilot.propose_slots",
    )


# ================================================================ copilot.prepare_pack
class PreparePackIn(In):
    application_id: uuid.UUID
    client_template_id: uuid.UUID | None = None


class PackCheckOut(Out):
    key: str
    ok: bool
    message: str


class PreparePackOut(Out):
    pack_id: uuid.UUID
    status: str
    checks: list[PackCheckOut]
    missing: list[str]


async def _prepare_pack(tctx: Any, args: PreparePackIn) -> ToolOutcome:
    from app.services import submission_packs_service

    await require_readable(tctx, "application", args.application_id)
    try:
        out = await submission_packs_service.prepare_pack(
            tctx.request, tctx.db, args.application_id, client_template_id=args.client_template_id
        )
    except NotImplementedError as exc:
        raise ConflictError(NOT_AVAILABLE, code="not_available") from exc
    data = PreparePackOut(
        pack_id=out.pack_id, status=out.status,
        checks=[PackCheckOut(key=c.key, ok=c.ok, message=c.message[:300]) for c in out.checks],
        missing=[m[:200] for m in out.missing],
    )  # fmt: skip
    return ToolOutcome(
        data, target=("application", args.application_id), summary=f"submission pack {out.status}"
    )


async def _pack_preview(tctx: Any, args: PreparePackIn) -> ProposalPreview:  # ruff: ignore[unused-function-argument]
    return ProposalPreview(
        title="Prepare a submission pack",
        summary="Assemble a draft submission pack and run the pre-send checks. Sending it to the client stays with you.",
        fields=(("Client template", "Default" if args.client_template_id is None else "Selected template"),),
        renderer="copilot.prepare_pack",
    )


register_tool(
    ToolSpec(
        key="copilot.propose_slots",
        version=1,
        owner_epic=OWNER_EPIC,
        title="Propose interview slots",
        description=(
            "Draft interview slot options for an application you can see (duration, interviewers, candidate time "
            "zone). The user confirms the draft; it never books an interview and never sends anything to the "
            "candidate or interviewers."
        ),
        input_model=ProposeSlotsIn,
        output_model=ProposeSlotsOut,
        permissions=("interview:create",),
        risk="low",
        scope=ScopeRule(resource="application", target_arg="application_id", cross_subject=True),
        handler=_propose_slots,
        preview=_slots_preview,
        default_mode="suggest",
    )
)

register_tool(
    ToolSpec(
        key="copilot.prepare_pack",
        version=1,
        owner_epic=OWNER_EPIC,
        title="Prepare a submission pack",
        description=(
            "Draft a submission pack for an application you can see and list its pre-send checks and missing items. "
            "The user confirms the draft; it never submits the candidate or sends anything to the client."
        ),
        input_model=PreparePackIn,
        output_model=PreparePackOut,
        permissions=("submission:create",),
        risk="low",
        scope=ScopeRule(resource="application", target_arg="application_id", cross_subject=True),
        handler=_prepare_pack,
        preview=_pack_preview,
        default_mode="suggest",
    )
)
