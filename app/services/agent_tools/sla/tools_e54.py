# ruff: file-ignore[line-too-long]
"""Epic 54 Copilot read tool ``sla.at_risk`` (FR-277): open demands you can see whose SLA is at risk (or breached),
with the worst open clock -- built on ``demands.list`` (scope = the user's)."""

import uuid
from typing import Any

from pydantic import Field

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.services.agent_tools.copilot.common_e54 import OWNER_EPIC, In, Out, plural, url_for

SEVERITY = {"breached": 2, "at_risk": 1}


class SlaAtRiskIn(In):
    include_breached: bool = True
    limit: int = Field(default=10, ge=1, le=25)


class SlaRow(Out):
    demand_id: uuid.UUID
    human_id: str | None
    title: str
    client_name: str | None
    milestone: str | None
    sla_status: str
    due_at: str | None
    owner_id: uuid.UUID | None
    url: str | None


class SlaAtRiskOut(Out):
    total: int
    rows: list[SlaRow]


def _worst(clocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    open_clocks = [c for c in clocks if not c.get("completed_at") and c.get("status") in SEVERITY]
    if not open_clocks:
        return None
    return sorted(open_clocks, key=lambda c: (-SEVERITY[str(c["status"])], str(c.get("due_at") or "")))[0]


async def _at_risk(tctx: Any, args: SlaAtRiskIn) -> ToolOutcome:
    from app.services.agent_tools.demands.tools_e54 import DemandListIn, list_rows

    statuses = ["breached", "at_risk"] if args.include_breached else ["at_risk"]
    total = 0
    rows: list[SlaRow] = []
    for status in statuses:
        count, items = await list_rows(
            tctx, DemandListIn(status=["open"], limit=args.limit), sla_status=status
        )
        total += count
        for d in items:
            clock = _worst(list(d.get("sla") or []))
            rows.append(
                SlaRow(
                    demand_id=d["id"], human_id=d.get("req_id"), title=str(d.get("title") or "")[:200],
                    client_name=d.get("client_name"), milestone=str(clock["milestone"]) if clock else None,
                    sla_status=str(d.get("sla_status") or status), due_at=str(clock["due_at"]) if clock and clock.get("due_at") else None,
                    owner_id=d.get("owner_id"), url=url_for("demand", d["id"]),
                )
            )  # fmt: skip
    rows = rows[: args.limit]
    return ToolOutcome(
        SlaAtRiskOut(total=total, rows=rows),
        refs=tuple(("demand", r.demand_id) for r in rows),
        summary=plural(total, "demand") + " at risk",
    )


register_tool(
    ToolSpec(
        key="sla.at_risk",
        mcp_exposed=True,
        version=1,
        owner_epic=OWNER_EPIC,
        title="Checked SLA risk",
        description=(
            "List open demands you can see whose SLA is at risk (and, by default, breached), with the worst open "
            "milestone and its due time. Counts only demands you can see. It changes nothing, extends no deadline "
            "and notifies no one."
        ),
        input_model=SlaAtRiskIn,
        output_model=SlaAtRiskOut,
        permissions=("demand:read",),
        risk="read",
        scope=ScopeRule(resource="demand", cross_subject=True),
        handler=_at_risk,
        untrusted_output=frozenset({"rows.title", "rows.client_name"}),
    )
)
