# ruff: noqa: E501
"""Epic 54 Copilot read tool ``matching.rank`` (FR-277; coordinator decision C-8: Copilot owns ``matching.rank``,
distinct from Epic 56's ``matching.list_ranked_matches``).

Stored P1 deterministic scores only (``matching.service.list_matches``, which checks the demand is visible): it never
triggers a match run. Rows whose candidate the user cannot read are dropped (NFR-46); suppressed scores (AEDT) stay
hidden. Band: strong >= 75, good >= 50, else weak.
"""

import uuid
from typing import Any

from pydantic import Field

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.services import entity_refs
from app.services.agent_tools.copilot.common_e54 import OWNER_EPIC, In, Out, scoped, url_for


class RankIn(In):
    demand_id: uuid.UUID
    limit: int = Field(default=10, ge=1, le=20)


class RankRow(Out):
    candidate_id: uuid.UUID
    full_name: str | None
    score: float | None
    band: str | None
    reasons: list[str]
    url: str | None


class RankOut(Out):
    rows: list[RankRow]


def band_of(score: float | None) -> str | None:
    if score is None:
        return None
    return "strong" if score >= 75 else "good" if score >= 50 else "weak"


async def _rank(tctx: Any, args: RankIn) -> ToolOutcome:
    from app.services.matching import service

    ctx = scoped(tctx)
    out = await service.list_matches(ctx, tctx.db, args.demand_id, page=1, page_size=min(50, args.limit * 3))
    rows: list[RankRow] = []
    for m in out.items:
        if len(rows) >= args.limit:
            break
        cid = m.candidate.id
        if not await entity_refs.can_read(ctx, tctx.db, "candidate", cid):
            continue
        reasons = [str(e.text)[:160] for e in m.evidence if e.text][:3]
        if not reasons and m.missing_mandatory:
            reasons = [f"Missing: {', '.join(m.missing_mandatory[:3])}"[:160]]
        rows.append(
            RankRow(
                candidate_id=cid, full_name=m.candidate.display_name, score=m.overall, band=band_of(m.overall),
                reasons=reasons, url=url_for("candidate", cid),
            )
        )  # fmt: skip
    ids = tuple(r.candidate_id for r in rows)
    return ToolOutcome(
        RankOut(rows=rows),
        refs=(("demand", args.demand_id), *(("candidate", i) for i in ids)),
        candidate_ids=ids,
        summary=f"ranked matches: {len(rows)}",
    )


register_tool(
    ToolSpec(
        key="matching.rank",
        mcp_exposed=True,
        version=1,
        owner_epic=OWNER_EPIC,
        title="Ranked matches",
        description=(
            "Top stored match scores for a demand you can see: candidate, score (0-100), band and up to three "
            "reasons. Uses the scores already computed; it never starts a new match run, never shortlists, submits "
            "or contacts anyone, and omits candidates outside your access."
        ),
        input_model=RankIn,
        output_model=RankOut,
        permissions=("demand:read", "candidate:read"),
        risk="read",
        scope=ScopeRule(resource="demand", target_arg="demand_id", cross_subject=True),
        handler=_rank,
        untrusted_output=frozenset({"rows.reasons"}),
    )
)
