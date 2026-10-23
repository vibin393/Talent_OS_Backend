# ruff: file-ignore[line-too-long]
"""Epic 54 Copilot read tool ``placements.list`` (FR-277): placements you can see (``placements.lists``; scope =
recruiter / account manager within ``placement:read``). No rates are returned. It never changes a placement."""

import uuid
from typing import Any, Literal

from pydantic import Field

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.core.pagination import PageParams
from app.services.agent_tools.copilot.common_e54 import OWNER_EPIC, In, Out, plural, scoped, url_for

PlacementStatus = Literal["pre_joining", "joining_confirmed", "active", "offboarding", "ended", "dropped"]


class PlacementListIn(In):
    status: list[PlacementStatus] = Field(default_factory=list, max_length=5)
    client_id: uuid.UUID | None = None
    ending_within_days: int | None = Field(default=None, ge=1, le=365)
    limit: int = Field(default=10, ge=1, le=25)


class PlacementRow(Out):
    id: uuid.UUID
    human_id: str | None
    candidate_name: str | None
    client_name: str | None
    role_title: str | None
    status: str
    planned_start_date: str | None
    effective_end_date: str | None
    url: str | None


class PlacementListOut(Out):
    total: int
    rows: list[PlacementRow]


async def _list(tctx: Any, args: PlacementListIn) -> ToolOutcome:
    from app.services.placements import lists

    page = await lists.list_placements(
        scoped(tctx), tctx.db, PageParams(page=1, page_size=args.limit), client_id=args.client_id,
        statuses=list(args.status) or None, ending_within_days=args.ending_within_days,
    )  # fmt: skip
    rows = [
        PlacementRow(
            id=p.id, human_id=p.human_id, candidate_name=p.candidate_name, client_name=p.client_name, role_title=p.role_title,
            status=p.status, planned_start_date=p.planned_start_date.isoformat() if p.planned_start_date else None,
            effective_end_date=(p.end_date or p.planned_end_date).isoformat() if (p.end_date or p.planned_end_date) else None,
            url=url_for("placement", p.id),
        )
        for p in page.items
    ]  # fmt: skip
    refs: list[tuple[str, uuid.UUID]] = [("placement", p.id) for p in page.items]
    refs += [("candidate", p.candidate_id) for p in page.items]
    return ToolOutcome(
        PlacementListOut(total=page.total, rows=rows),
        refs=tuple(refs),
        candidate_ids=tuple(dict.fromkeys(p.candidate_id for p in page.items)),
        summary=plural(page.total, "placement"),
    )


register_tool(
    ToolSpec(
        key="placements.list",
        mcp_exposed=True,
        version=1,
        owner_epic=OWNER_EPIC,
        title="Listed placements",
        description=(
            "List placements you can see by status, client or ending within N days. Returns the total and the top "
            "rows (no rates). It never starts, extends, ends or changes a placement."
        ),
        input_model=PlacementListIn,
        output_model=PlacementListOut,
        permissions=("placement:read",),
        risk="read",
        scope=ScopeRule(resource="placement", cross_subject=True),
        handler=_list,
    )
)
