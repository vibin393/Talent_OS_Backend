# ruff: noqa: E501
"""Epic 56 Sourcing Agent tool ``pipeline.add_candidate`` (risk ``high``: only ever a Proposal).

It is offered only when the recruiter enabled "Let the agent propose pipeline adds" (checked when the Proposal is
created). On approval it runs as the approving person: P1 ``pipeline_service.add_candidate(source="ai_match",
source_ref="agent_shortlist:<entry>")``; the shortlist entry becomes ``added_to_pipeline`` and
``AgentShortlistEntryDecided(via_proposal=True)`` is emitted (the audit row carries ``via_proposal_id``). It never
moves an Application (the *Shortlisted* stage and every later stage stay with people).
"""

import uuid
from typing import Any

from pydantic import Field

from app.core.agentic import ProposalPreview, ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.core.errors import ValidationFailed
from app.services.agent_tools.sourcing.common_e56 import OWNER_EPIC, VERSION, In, Out, scoped


class AddIn(In):
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    entry_id: uuid.UUID | None = None
    reason: str = Field(min_length=1, max_length=300)


class AddOut(Out):
    application_id: uuid.UUID


async def _in_pipeline(probe: Any) -> str:
    from app.services import pipeline_service

    ids = await pipeline_service.candidate_ids_for_demand(probe.tctx.request, probe.db, probe.args.demand_id)
    return f"in_pipeline:{probe.args.candidate_id in ids}"


async def _add(tctx: Any, args: AddIn) -> ToolOutcome:
    from app.services import sourcing_agent_service as sourcing

    if tctx.execution == "agent":  # defence in depth: risk 'high' never reaches auto
        raise ValidationFailed("Pipeline adds need a person's approval", code="proposal_required")
    application_id = await sourcing.add_via_proposal(
        tctx.request, tctx.db, args.demand_id, args.candidate_id, args.entry_id
    )
    return ToolOutcome(
        AddOut(application_id=application_id), refs=(("application", application_id),),
        candidate_ids=(args.candidate_id,), target=("demand", args.demand_id), summary="added to the pipeline",
    )  # fmt: skip


async def _preview(tctx: Any, args: AddIn) -> ProposalPreview:
    from app.services import entity_refs
    from app.services import sourcing_agent_service as sourcing

    if tctx.execution == "agent":
        settings = await sourcing.get_settings(scoped(tctx), tctx.db)
        if not settings.propose_pipeline_adds:
            raise ValidationFailed(
                "Pipeline-add proposals are turned off for the Sourcing Agent", code="pipeline_adds_disabled"
            )
        if not await entity_refs.can_read(scoped(tctx), tctx.db, "candidate", args.candidate_id):
            from app.core.errors import NotFound

            raise NotFound("Candidate not found")
    name = await entity_refs.entity_label(tctx.db, "candidate", args.candidate_id) or "the candidate"
    demand = await entity_refs.entity_label(tctx.db, "demand", args.demand_id) or "the Demand"
    return ProposalPreview(
        title=f"Add {name} to the pipeline of {demand}"[:200],
        summary=args.reason[:4000],
        fields=(("Candidate", name), ("Demand", demand), ("demand_id", str(args.demand_id))),
        renderer="pipeline.add_candidate",
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        key="pipeline.add_candidate", mcp_exposed=True, version=VERSION, owner_epic=OWNER_EPIC, title="Add to pipeline",
        description="Propose adding a shortlisted candidate to the Demand's pipeline (first stage) with a short reason. A person approves it; only use when the run settings allow pipeline-add proposals.",
        input_model=AddIn, output_model=AddOut, permissions=("application:create", "demand:read"), risk="high",
        scope=ScopeRule(resource="demand", target_arg="demand_id", candidate_arg="candidate_id", version=_in_pipeline),
        handler=_add, preview=_preview,
    ),
)  # fmt: skip


def register() -> None:
    from app.core.agentic.tools import tools

    for spec in TOOLS:
        if spec.key not in tools():
            register_tool(spec)


register()
