# ruff: noqa: E501
"""Epic 54 Copilot read tool ``interviews.list`` (FR-277): interviews of a demand or an application you can see, or
your own (organiser / participant) -- ``interviews_service.list_for_demand`` / ``list_for_application`` /
``list_mine``. It never schedules, cancels or records an outcome."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.core.pagination import PageParams
from app.services.agent_tools.copilot.common_e54 import OWNER_EPIC, In, Out, plural, scoped, url_for


class InterviewListIn(In):
    demand_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None
    mine: bool = False
    date_from: datetime | None = None
    date_to: datetime | None = None
    status: Literal["scheduled", "completed", "cancelled", "no_show"] | None = None
    limit: int = Field(default=10, ge=1, le=25)


class InterviewRow(Out):
    id: uuid.UUID
    application_id: uuid.UUID | None
    candidate_name: str | None
    demand_title: str | None
    starts_at: str
    status: str
    stage_name: str | None
    scorecard_status: str
    url: str | None


class InterviewListOut(Out):
    total: int
    rows: list[InterviewRow]


def _scorecards(i: Any) -> str:
    required = int(getattr(i, "scorecards_required", 0) or 0)
    submitted = int(getattr(i, "scorecards_submitted", 0) or 0)
    if required == 0:
        return "none_required"
    return "complete" if submitted >= required else f"{submitted} of {required} submitted"


async def _list(tctx: Any, args: InterviewListIn) -> ToolOutcome:
    from app.services import interviews_service

    ctx = scoped(tctx)
    params = PageParams(page=1, page_size=args.limit)
    if args.application_id is not None:
        items = await interviews_service.list_for_application(ctx, tctx.db, args.application_id)
        items = [
            i for i in items
            if (args.status is None or i.status == args.status)
            and (args.date_from is None or i.starts_at >= args.date_from)
            and (args.date_to is None or i.starts_at <= args.date_to)
        ]  # fmt: skip
        total = len(items)
        items = items[: args.limit]
    elif args.demand_id is not None and not args.mine:
        page = await interviews_service.list_for_demand(
            ctx,
            tctx.db,
            args.demand_id,
            params,
            date_from=args.date_from,
            date_to=args.date_to,
            status=args.status,
        )
        total, items = page.total, list(page.items)
    else:
        page = await interviews_service.list_mine(
            ctx, tctx.db, params, date_from=args.date_from, date_to=args.date_to, status=args.status
        )
        items = [i for i in page.items if args.demand_id is None or i.demand_id == args.demand_id]
        total = page.total if args.demand_id is None else len(items)
    rows = [
        InterviewRow(
            id=i.id, application_id=i.application_id, candidate_name=i.candidate_name, demand_title=i.demand_title,
            starts_at=i.starts_at.isoformat(), status=str(i.status), stage_name=i.stage_name, scorecard_status=_scorecards(i),
            url=url_for("interview", i.id),
        )
        for i in items
    ]  # fmt: skip
    refs: list[tuple[str, uuid.UUID]] = [("interview", r.id) for r in rows]
    refs += [("application", r.application_id) for r in rows if r.application_id]
    cands = tuple(dict.fromkeys(i.candidate_id for i in items if getattr(i, "candidate_id", None)))
    return ToolOutcome(
        InterviewListOut(total=total, rows=rows),
        refs=tuple(refs),
        candidate_ids=cands,
        summary=plural(total, "interview"),
    )


register_tool(
    ToolSpec(
        key="interviews.list",
        mcp_exposed=True,
        version=1,
        owner_epic=OWNER_EPIC,
        title="Listed interviews",
        description=(
            "List interviews of a demand or an application you can see, or your own interviews (mine=true, or when "
            "neither id is given), optionally between date_from/date_to and by status. Shows the scorecard state. It never "
            "schedules, reschedules, cancels or records an interview outcome."
        ),
        input_model=InterviewListIn,
        output_model=InterviewListOut,
        permissions=("interview:read",),
        risk="read",
        scope=ScopeRule(resource="interview", cross_subject=True),
        handler=_list,
    )
)
