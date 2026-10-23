# ruff: file-ignore[line-too-long]
"""AI governance console (Epic 65, spec-p4-governance A1-A5; FR-319..FR-322, FR-325, FR-330).

``/ai-governance/*`` -- run explorer, run detail with timeline (Copilot runs metadata only), tool-call search, policy
violations, costs, summary, evaluation results and the Proposal "opened" signal. ``/agents/{key}/checklist``,
``/agents/{key}/limits``, ``/agents/{key}/burn-in/end`` and ``/agents/{key}/checklist/acknowledge``.

Internal principals; API keys may use the GETs (FR-329). Writes are ★ human only (403 ``human_only_action``).
No endpoint returns Copilot or Analytics question / answer text.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.pagination import Page
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.agentic import AgentCatalogOut, RunSummary
from app.schemas.ai_governance import (
    AgentLimitsOut,
    ChecklistOut,
    CostQuery,
    CostsOut,
    EvalRowOut,
    GovernanceRunQuery,
    GovernanceSummaryOut,
    ReasonIn,
    RunDetailOut,
    ToolCallQuery,
    ToolCallRow,
    ViolationQuery,
    ViolationRow,
)
from app.services.agent_runtime.governance import burn_in, console, signals
from app.services.ai_governance import checklist, evals

router = APIRouter(tags=["ai-governance"])
DB = TenantDBSessionDep
Govern = Annotated[Principal, Depends(require("ai:govern"))]
GovernOrConfigure = Annotated[Principal, Depends(require_any("ai:govern", "ai:agent_configure"))]
Configure = Annotated[Principal, Depends(require("ai:agent_configure"))]
DecideOrGovern = Annotated[Principal, Depends(require_any("proposal:decide", "ai:govern"))]


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


@router.get("/ai-governance/summary", response_model=GovernanceSummaryOut)
async def summary(_: Govern, db: DB) -> GovernanceSummaryOut:
    return await console.summary(get_context(), db)


@router.get("/ai-governance/runs", response_model=Page[RunSummary])
async def list_runs(_: Govern, db: DB, q: Annotated[GovernanceRunQuery, Query()]) -> Page[RunSummary]:
    return await console.list_runs(get_context(), db, q)


@router.get("/ai-governance/runs/{run_id}", response_model=RunDetailOut)
async def run_detail(run_id: uuid.UUID, _: Govern, db: DB) -> RunDetailOut:
    return await console.run_detail(get_context(), db, run_id)


@router.get("/ai-governance/tool-calls", response_model=Page[ToolCallRow])
async def tool_calls(_: Govern, db: DB, q: Annotated[ToolCallQuery, Query()]) -> Page[ToolCallRow]:
    return await console.search_tool_calls(get_context(), db, q)


@router.get("/ai-governance/violations", response_model=Page[ViolationRow])
async def violations(_: Govern, db: DB, q: Annotated[ViolationQuery, Query()]) -> Page[ViolationRow]:
    return await console.violations(get_context(), db, q)


@router.get("/ai-governance/costs", response_model=CostsOut)
async def costs(_: Govern, db: DB, q: Annotated[CostQuery, Query()]) -> CostsOut:
    return await console.costs(get_context(), db, q)


@router.get("/ai-governance/evals", response_model=list[EvalRowOut])
async def eval_results(_: GovernOrConfigure, db: DB) -> list[EvalRowOut]:
    return await evals.latest_for_tenant(get_context(), db)


@router.post("/ai-governance/proposals/{proposal_id}/opened", status_code=status.HTTP_204_NO_CONTENT)
async def proposal_opened(proposal_id: uuid.UUID, _: DecideOrGovern, db: DB) -> Response:
    _human()
    await signals.mark_opened(get_context(), db, proposal_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- per-agent settings additions
@router.get("/agents/{key}/checklist", response_model=ChecklistOut)
async def agent_checklist(key: str, _: GovernOrConfigure, db: DB) -> ChecklistOut:
    return await checklist.evaluate(get_context(), db, key)


@router.get("/agents/{key}/limits", response_model=AgentLimitsOut)
async def agent_limits(key: str, _: GovernOrConfigure, db: DB) -> AgentLimitsOut:
    from sqlalchemy import select

    from app.core.agentic.agents import get_agent
    from app.core.errors import NotFound
    from app.models.ai import AITenantSettings

    try:
        spec = get_agent(key)
    except KeyError as exc:
        raise NotFound("Agent not found") from exc
    budget = (await db.execute(select(AITenantSettings.monthly_token_budget))).scalar_one_or_none()
    return AgentLimitsOut(agent_key=key, ceiling=spec.limits.to_json(), tenant_monthly_token_budget=budget)


@router.post("/agents/{key}/burn-in/end", response_model=AgentCatalogOut)
async def end_burn_in(key: str, body: ReasonIn, _: Configure, db: DB) -> AgentCatalogOut:
    _human()
    return await burn_in.end_early(get_context(), db, key, body.reason)


@router.post("/agents/{key}/checklist/acknowledge", status_code=status.HTTP_204_NO_CONTENT)
async def acknowledge_checklist(key: str, body: ReasonIn, _: Configure, db: DB) -> Response:
    _human()
    await burn_in.acknowledge_checklist(get_context(), db, key, body.reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
