# ruff: noqa: E501
"""Epic 70 job board tools (spec A3, "Agents and MCP").

| Key | Risk | Notes |
|---|---|---|
| ``job_boards.publish`` | high, egress | publish a live careers posting to boards; runs only as an approved Proposal, as the approver |
| ``job_boards.status`` | read | the Board Postings of a demand |

Handlers call only the ``job_distribution_service`` facade.
"""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.agentic import ProposalPreview, ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.core.errors import Forbidden

OWNER_EPIC = "70"


class PublishToolIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    demand_id: uuid.UUID
    board_keys: list[str] = Field(min_length=1, max_length=10)
    board_fields: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PostingRowOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    board_key: str
    status: str
    board_url: str | None
    out_of_date: bool


class PublishToolOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    published: list[PostingRowOut]
    blocked_boards: list[str]


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    demand_id: uuid.UUID


class StatusOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    postings: list[PostingRowOut]


def _row(p: Any) -> PostingRowOut:
    return PostingRowOut(id=p.id, board_key=p.board_key, status=p.status, board_url=p.board_url, out_of_date=p.out_of_date)


async def _publish(tctx: Any, args: PublishToolIn) -> ToolOutcome:
    from app.schemas.job_distribution import PublishIn
    from app.services import job_distribution_service as svc

    if tctx.execution != "approved_proposal":  # defence in depth: high risk never runs directly
        raise Forbidden("Publishing to job boards needs a person's approval", code="human_only_action")
    out = await svc.publish(
        tctx.request, tctx.db, args.demand_id,
        PublishIn(board_keys=args.board_keys, board_fields=args.board_fields, acknowledge_bias_flags=False),
        approved_proposal=True,
    )  # fmt: skip
    data = PublishToolOut(published=[_row(p) for p in out.published], blocked_boards=[b.board_key for b in out.blocked])
    return ToolOutcome(data, target=("demand", args.demand_id), summary=f"sent to {len(data.published)} board(s)")


async def _publish_preview(tctx: Any, args: PublishToolIn) -> ProposalPreview:  # noqa: ARG001
    return ProposalPreview(
        title="Publish the job to external job boards",
        summary="Publishes the approved careers posting to: " + ", ".join(args.board_keys),
        fields=tuple(("Board", k) for k in args.board_keys),
    )


async def _status(tctx: Any, args: StatusIn) -> ToolOutcome:
    from app.services import job_distribution_service as svc

    rows = await svc.list_for_demand(tctx.request, tctx.db, args.demand_id)
    return ToolOutcome(StatusOut(postings=[_row(p) for p in rows]), target=("demand", args.demand_id), summary=f"{len(rows)} board posting(s)")


_DEMAND = ScopeRule(resource="demand", target_arg="demand_id")

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        key="job_boards.publish", mcp_exposed=True, version=1, owner_epic=OWNER_EPIC, title="Publish to job boards",
        description="Propose publishing a demand's live careers posting to external job boards (Naukri, LinkedIn, Indeed, the XML feed). Always a proposal a person approves; the approved JD and the posting's visibility settings are used. Boards with missing required fields are skipped.",
        input_model=PublishToolIn, output_model=PublishToolOut, permissions=("posting:publish_external",), risk="high", scope=_DEMAND,
        handler=_publish, preview=_publish_preview, max_calls_per_run=3,  # no egress flag: a board post is not a message (send_intent)
    ),
    ToolSpec(
        key="job_boards.status", mcp_exposed=True, version=1, owner_epic=OWNER_EPIC, title="Job board postings",
        description="List the job board postings of a demand with their status (live, expiring, closed, failed...). It changes nothing.",
        input_model=StatusIn, output_model=StatusOut, permissions=("demand:read",), risk="read", scope=_DEMAND, handler=_status,
    ),
)  # fmt: skip


def register() -> None:
    from app.core.agentic.tools import tools

    for spec in TOOLS:
        if spec.key not in tools():
            register_tool(spec)


register()
