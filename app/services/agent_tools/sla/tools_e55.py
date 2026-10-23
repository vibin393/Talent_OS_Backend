# ruff: file-ignore[line-too-long]
"""Epic 55 SLA-risk tools (A3, FR-284): ``sla.status`` (one demand; the PRD's ``predictions.time_to_fill`` folded
in), ``sla.risk_scan`` (at-risk open demands visible to the scan owner) and ``sla.send_digest`` (one digest per
recipient, filtered by each recipient's own access; internal only). None changes a Demand field."""

import uuid
from decimal import Decimal
from typing import Any

from pydantic import Field

from app.core.agentic import ProposalPreview, ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.schemas.demand_insights import NextStep, SlaClockOut
from app.services.agent_tools.demands.common_e55 import OWNER_EPIC, In, Out, scoped


class DemandIn(In):
    demand_id: uuid.UUID


class StatusOut(Out):
    at_risk: bool
    basis: str
    basis_text: str
    p_late: Decimal | None
    milestone: str | None
    clocks: list[SlaClockOut]
    counts: dict[str, int]
    reasons: list[str]
    next_steps: list[NextStep]


async def _status(tctx: Any, args: DemandIn) -> ToolOutcome:
    from app.services import demand_insights_service as insights

    risk = await insights.sla_risk.assess(scoped(tctx), tctx.db, args.demand_id)
    out = StatusOut(**risk.model_dump(exclude={"demand_id"}))
    return ToolOutcome(
        out,
        target=("demand", args.demand_id),
        summary=f"sla {'at risk' if out.at_risk else 'on track'} ({out.basis})",
    )


class ScanIn(In):
    limit: int | None = Field(default=None, ge=1, le=100)


class ScanItem(Out):
    demand_id: uuid.UUID
    human_id: str
    title: str
    client_name: str | None
    milestone: str | None
    basis: str
    p_late: Decimal | None
    reasons: list[str]


class ScanOut(Out):
    items: list[ScanItem]
    scanned: int


async def _scan(tctx: Any, args: ScanIn) -> ToolOutcome:
    from app.services import clients_service
    from app.services import demand_insights_service as insights

    found, scanned = await insights.sla_risk.scan(scoped(tctx), tctx.db, limit=args.limit)
    items = [
        ScanItem(
            demand_id=d.id,
            human_id=d.req_id,
            title=d.title[:300],
            client_name=await clients_service.client_name(tctx.db, d.client_id),
            milestone=r.milestone,
            basis=r.basis,
            p_late=r.p_late,
            reasons=r.reasons[:3],
        )
        for d, r in found
    ]
    return ToolOutcome(
        ScanOut(items=items, scanned=scanned), refs=tuple(("demand", i.demand_id) for i in items),
        summary=f"{len(items)} of {scanned} demands at risk",
    )  # fmt: skip


class DigestItem(In):
    demand_id: uuid.UUID
    note: str | None = Field(default=None, max_length=300)


class DigestIn(In):
    items: list[DigestItem] = Field(min_length=1, max_length=100)


class DigestOut(Out):
    digests_sent: int
    flags_raised: int


async def _digest(tctx: Any, args: DigestIn) -> ToolOutcome:
    from app.services import demand_insights_service as insights

    sent, raised = await insights.sla_risk.send_digest(
        scoped(tctx), tctx.db, [(i.demand_id, i.note) for i in args.items], run_id=tctx.run_id
    )
    return ToolOutcome(DigestOut(digests_sent=sent, flags_raised=raised), summary=f"{sent} digest(s) sent")


async def _digest_preview(tctx: Any, args: DigestIn) -> ProposalPreview:  # ruff: ignore[unused-function-argument]
    return ProposalPreview(
        title=f"Send the daily SLA risk digest ({len(args.items)} demands)",
        summary="One in-app / email digest per recipient (primary recruiter and the business-unit managers), listing only demands each recipient can see.",
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        key="sla.status", version=1, owner_epic=OWNER_EPIC, title="Check SLA risk",
        description="Check whether a demand is likely to miss its SLA: open clocks, pipeline counts, the time-to-fill prediction when available (else pipeline progress only), reasons and next-step links. It changes nothing.",
        input_model=DemandIn, output_model=StatusOut, permissions=("demand:read",), risk="read",
        scope=ScopeRule(resource="demand", target_arg="demand_id"), handler=_status,
    ),
    ToolSpec(
        key="sla.risk_scan", version=1, owner_epic=OWNER_EPIC, title="Scan for SLA risk",
        description="List open demands you can see that are likely to miss their SLA, most at risk first (at most the digest size). It changes nothing.",
        input_model=ScanIn, output_model=ScanOut, permissions=("demand:read",), risk="read",
        scope=ScopeRule(resource="none"), handler=_scan, untrusted_output=frozenset({"items.title", "items.client_name"}),
    ),
    ToolSpec(
        key="sla.send_digest", version=1, owner_epic=OWNER_EPIC, title="Send the SLA risk digest",
        description="Send the daily SLA-risk digest for the given demands (re-checked by the platform; demands not at risk are ignored). Recipients are chosen by the platform and each sees only demands they can access. Internal only; it changes no demand.",
        input_model=DigestIn, output_model=DigestOut, permissions=("demand:read",), risk="low",
        scope=ScopeRule(resource="none"), handler=_digest, preview=_digest_preview, default_mode="auto", max_calls_per_run=1,
    ),
)  # fmt: skip


def register() -> None:
    from app.core.agentic.tools import tools

    for spec in TOOLS:
        if spec.key not in tools():
            register_tool(spec)


register()
