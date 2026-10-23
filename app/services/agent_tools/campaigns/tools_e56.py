# ruff: file-ignore[line-too-long]
"""Epic 56 Sourcing Agent tools of the ``campaigns`` domain (FR-288).

* ``messaging.cap_status`` (read) -- per-channel contactability of up to 25 candidates under the P3 marketing
  governance rules (consent, opt-outs, WhatsApp opt-in, address, bounce / complaint, the 3-per-7-days cap).
* ``sequences.enrol`` (high, egress, templated) -- a Proposal to enrol shortlisted candidates in the Sequence the
  recruiter chose in the Sourcing settings; candidates suppressed by the governance are listed with reasons in the
  preview. On approval it runs as the approving person (``campaigns.sequences.enrol(origin="user")``); the Sequence
  steps pass the governance again at every send.
"""

import uuid
from typing import Any

from pydantic import Field

from app.core.agentic import (
    ProposalPreview,
    Recipient,
    ScopeRule,
    SendIntent,
    ToolOutcome,
    ToolSpec,
    register_tool,
)
from app.core.errors import NotFound, ValidationFailed
from app.services.agent_tools.sourcing.common_e56 import OWNER_EPIC, VERSION, In, Out, scoped


# ================================================================ messaging.cap_status
class CapIn(In):
    demand_id: uuid.UUID
    candidate_ids: list[uuid.UUID] = Field(min_length=1, max_length=25)


class ChannelOk(Out):
    ok: bool
    reason: str | None


class CapItem(Out):
    candidate_id: uuid.UUID
    email: ChannelOk
    sms: ChannelOk
    whatsapp: ChannelOk
    best_channel: str | None


class CapOut(Out):
    items: list[CapItem]


async def _cap(tctx: Any, args: CapIn) -> ToolOutcome:
    from app.services import sourcing_agent_service as sourcing

    states = await sourcing.cap_status(
        scoped(tctx), tctx.db, args.demand_id, list(dict.fromkeys(args.candidate_ids))
    )
    items = [
        CapItem(
            candidate_id=cid,
            email=ChannelOk(**c.email.model_dump()),
            sms=ChannelOk(**c.sms.model_dump()),
            whatsapp=ChannelOk(**c.whatsapp.model_dump()),
            best_channel=c.best_channel,
        )
        for cid, c in states.items()
    ]
    return ToolOutcome(
        CapOut(items=items), candidate_ids=tuple(states), target=("demand", args.demand_id),
        summary=f"contactability of {len(items)} candidates",
    )  # fmt: skip


# ================================================================ sequences.enrol
class EnrolIn(In):
    sequence_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_ids: list[uuid.UUID] = Field(min_length=1, max_length=25)


class Refusal(Out):
    candidate_id: uuid.UUID
    reason: str


class EnrolOut(Out):
    enrolled: int
    refused: list[Refusal]


async def _enrol_intent(tctx: Any, args: EnrolIn) -> SendIntent:  # ruff: ignore[unused-function-argument]
    return SendIntent(
        recipients=tuple(Recipient(kind="candidate", id=cid, channel="email") for cid in dict.fromkeys(args.candidate_ids)),
        templated=True, template_ref=f"sequence:{args.sequence_id}",
    )  # fmt: skip


async def _enrol(tctx: Any, args: EnrolIn) -> ToolOutcome:
    from app.services.campaigns import sequences

    if tctx.execution == "agent":  # defence in depth: risk 'high' never reaches auto
        raise ValidationFailed("Sequence enrolment needs a person's approval", code="proposal_required")
    out = await sequences.enrol(
        tctx.request, tctx.db, args.sequence_id, list(dict.fromkeys(args.candidate_ids)), origin="user"
    )
    return ToolOutcome(
        EnrolOut(enrolled=out.enrolled, refused=[Refusal(candidate_id=r.candidate_id, reason=r.reason) for r in out.refused]),
        candidate_ids=tuple(args.candidate_ids), target=("demand", args.demand_id),
        summary=f"{out.enrolled} enrolled",
    )  # fmt: skip


async def _enrol_preview(tctx: Any, args: EnrolIn) -> ProposalPreview:
    from app.models.campaigns import Sequence
    from app.services import entity_refs
    from app.services import sourcing_agent_service as sourcing

    sequence = await tctx.db.get(Sequence, args.sequence_id)
    if sequence is None:
        raise NotFound("Sequence not found")
    if tctx.execution == "agent":
        settings = await sourcing.get_settings(scoped(tctx), tctx.db)
        if settings.propose_sequence_id != args.sequence_id:
            raise ValidationFailed(
                "Enrolment proposals are only allowed for the Sequence chosen in the Sourcing settings",
                code="sequence_not_enabled",
            )
    states = await sourcing.cap_status(
        scoped(tctx), tctx.db, args.demand_id, list(dict.fromkeys(args.candidate_ids))
    )
    suppressed = [
        (cid, c.email.reason or "suppressed") for cid, c in states.items() if c.best_channel is None
    ]
    names = {
        cid: await entity_refs.entity_label(tctx.db, "candidate", cid) or str(cid)
        for cid in args.candidate_ids
    }
    lines = [f"Enrol {len(args.candidate_ids)} candidate(s) in the Sequence “{sequence.name}”."]
    if suppressed:
        lines.append(
            "Suppressed (will not receive messages): " + "; ".join(f"{names[c]} ({r})" for c, r in suppressed)
        )
    return ProposalPreview(
        title=f"Enrol {len(args.candidate_ids)} in {sequence.name}"[:200],
        summary="\n".join(lines)[:4000],
        fields=(
            ("Sequence", sequence.name),
            ("Candidates", ", ".join(names.values())[:500]),
            ("demand_id", str(args.demand_id)),
        ),
        renderer="sequences.enrol",
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        key="messaging.cap_status", version=VERSION, owner_epic=OWNER_EPIC, title="Contactability",
        description="Check, per channel (email, SMS, WhatsApp), whether up to 25 candidates may receive marketing outreach now (consent, opt-outs, address, bounces, weekly frequency cap) and the best permitted channel. Read only.",
        input_model=CapIn, output_model=CapOut, permissions=("candidate:read",), risk="read",
        scope=ScopeRule(resource="demand", target_arg="demand_id"), handler=_cap,
    ),
    ToolSpec(
        key="sequences.enrol", version=VERSION, owner_epic=OWNER_EPIC, title="Enrol in a Sequence",
        description="Propose enrolling shortlisted candidates (max 25) in the nurture Sequence chosen in the Sourcing settings. A person approves it; the Sequence messages are approved templates and pass consent and cap checks at every send.",
        input_model=EnrolIn, output_model=EnrolOut, permissions=("sequence:write",), risk="high",
        scope=ScopeRule(resource="demand", target_arg="demand_id"), handler=_enrol, preview=_enrol_preview,
        egress=True, send_intent=_enrol_intent,
    ),
)  # fmt: skip


def register() -> None:
    from app.core.agentic.tools import tools

    for spec in TOOLS:
        if spec.key not in tools():
            register_tool(spec)


register()
