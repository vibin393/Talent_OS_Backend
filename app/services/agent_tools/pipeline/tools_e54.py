# ruff: noqa: E501
"""Epic 54 Copilot read tool ``pipeline.list`` (FR-277): applications of one demand or one candidate you can see,
optionally by stage keys and idleness ("interviewed but not followed up": ``idle_days`` = no stage move for N days;
the pipeline has no separate last-activity column, so ``stage_entered_at`` is the activity marker).

Reads through ``pipeline.applications`` (``list_for_demand`` / ``candidate_applications``); it never moves a stage."""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import Field, model_validator

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.core.pagination import PageParams
from app.services.agent_tools.copilot.common_e54 import OWNER_EPIC, In, Out, plural, scoped, url_for

SCAN = 100


class PipelineListIn(In):
    demand_id: uuid.UUID | None = None
    candidate_id: uuid.UUID | None = None
    stage_keys: list[str] = Field(default_factory=list, max_length=10)
    idle_days: int | None = Field(default=None, ge=1, le=365)
    limit: int = Field(default=10, ge=1, le=25)

    @model_validator(mode="after")
    def _exactly_one(self) -> "PipelineListIn":
        if (self.demand_id is None) == (self.candidate_id is None):
            msg = "give exactly one of demand_id or candidate_id"
            raise ValueError(msg)
        return self


class PipelineRow(Out):
    id: uuid.UUID
    candidate_id: uuid.UUID | None
    candidate_name: str | None
    demand_id: uuid.UUID
    demand_title: str | None
    stage_key: str
    stage_label: str | None
    stage_entered_at: str
    days_in_stage: int
    owner_id: uuid.UUID | None
    url: str | None


class PipelineListOut(Out):
    total: int
    rows: list[PipelineRow]


def _days(at: datetime) -> int:
    return max(0, (datetime.now(UTC) - at).days)


async def _list(tctx: Any, args: PipelineListIn) -> ToolOutcome:
    from app.services.pipeline import applications as pipeline_service

    ctx = scoped(tctx)
    rows: list[PipelineRow] = []
    if args.demand_id is not None:
        page = await pipeline_service.list_for_demand(
            ctx, tctx.db, args.demand_id, PageParams(page=1, page_size=SCAN)
        )
        for a in page.items:
            rows.append(
                PipelineRow(
                    id=a.id, candidate_id=a.candidate.id, candidate_name=a.candidate.full_name, demand_id=a.demand_id,
                    demand_title=a.demand_title, stage_key=a.stage.key, stage_label=a.stage.label,
                    stage_entered_at=a.stage_entered_at.isoformat(), days_in_stage=_days(a.stage_entered_at),
                    owner_id=a.owner_id, url=url_for("application", a.id),
                )
            )  # fmt: skip
    else:
        assert args.candidate_id is not None
        for a in await pipeline_service.candidate_applications(ctx, tctx.db, args.candidate_id):
            rows.append(
                PipelineRow(
                    id=a.id, candidate_id=args.candidate_id, candidate_name=None, demand_id=a.demand_id,
                    demand_title=a.demand_title, stage_key=a.stage.key, stage_label=a.stage.label,
                    stage_entered_at=a.stage_entered_at.isoformat(), days_in_stage=_days(a.stage_entered_at),
                    owner_id=None, url=url_for("application", a.id),
                )
            )  # fmt: skip
    if args.stage_keys:
        wanted = set(args.stage_keys)
        rows = [r for r in rows if r.stage_key in wanted]
    if args.idle_days is not None:
        rows = [r for r in rows if r.days_in_stage >= args.idle_days]
    total = len(rows)
    rows = rows[: args.limit]
    refs: list[tuple[str, uuid.UUID]] = []
    for r in rows:
        refs.append(("application", r.id))
        refs.append(("demand", r.demand_id))
        if r.candidate_id:
            refs.append(("candidate", r.candidate_id))
    cands = tuple(dict.fromkeys(r.candidate_id for r in rows if r.candidate_id))
    return ToolOutcome(
        PipelineListOut(total=total, rows=rows),
        refs=tuple(refs),
        candidate_ids=cands,
        summary=plural(total, "application"),
    )


register_tool(
    ToolSpec(
        key="pipeline.list",
        mcp_exposed=True,
        version=1,
        owner_epic=OWNER_EPIC,
        title="Listed applications",
        description=(
            "List the applications of exactly one demand or one candidate you can see, optionally only some stage "
            "keys (e.g. 'interviewed') and only those idle in their stage for at least idle_days days. Returns the "
            "total (of the first 100) and the top rows. It never moves, rejects, shortlists or submits anyone."
        ),
        input_model=PipelineListIn,
        output_model=PipelineListOut,
        permissions=("application:read",),
        risk="read",
        scope=ScopeRule(resource="application", cross_subject=True),
        handler=_list,
        untrusted_output=frozenset({"rows.demand_title"}),
    )
)
