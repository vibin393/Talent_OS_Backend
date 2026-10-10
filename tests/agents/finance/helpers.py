# ruff: noqa: E501
"""Epic 63 agent test helpers: Fake-LLM runs of the ``timesheet`` / ``billing`` / ``collections`` agents."""

import uuid
from typing import Any

from sqlalchemy import select, update

from app.core.agentic import Kickoff
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.models.agent_runtime import AgentActionPolicy, AgentProposal, AgentRun, AgentToolCall
from app.schemas.agentic import ProposalDecisionIn
from app.services import entity_refs
from app.services.ai.fake_llm import FakeTurn, fake_llm


def turns(*raw: dict[str, Any]) -> list[FakeTurn]:
    return [FakeTurn.from_mapping(t) for t in raw]


def call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "tool_calls", "calls": [[tool, args]]}


def done(**result: Any) -> dict[str, Any]:
    return {"kind": "emit_result", "result": {"outcome": "done", "summary": "ok", **result}}


async def ctx_for(tenant_id: uuid.UUID, user_id: uuid.UUID) -> RequestContext:
    async with tenant_session(tenant_id) as db:
        principal = await entity_refs.principal_for_user(db, tenant_id, user_id)
    return RequestContext(tenant_id=tenant_id, principal=principal)


async def run_agent(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    agent_key: str,
    subject: tuple[str, uuid.UUID],
    script: list[dict[str, Any]],
    *,
    trigger_kind: str = "manual",
    trigger_key: str = "manual",
    kickoff: Kickoff | None = None,
    owner: uuid.UUID | None = None,
) -> Any:
    from app.services import agent_runtime_service as runtime

    ctx = await ctx_for(tenant_id, user_id)
    with fake_llm(turns(*script)):
        async with tenant_session(tenant_id) as db:
            return await runtime.start_run(
                ctx, db, agent_key=agent_key, trigger_kind=trigger_kind, trigger_key=trigger_key, subject=subject,
                owner_user_id=owner or user_id, kickoff=kickoff,
            )  # fmt: skip


async def run_row(tenant_id: uuid.UUID, run_id: uuid.UUID) -> AgentRun:
    async with tenant_session(tenant_id) as db:
        run = await db.get(AgentRun, run_id)
        assert run is not None
        db.expunge(run)
        return run


async def calls(tenant_id: uuid.UUID, run_id: uuid.UUID) -> list[AgentToolCall]:
    async with tenant_session(tenant_id) as db:
        rows = list(
            (
                await db.execute(
                    select(AgentToolCall).where(AgentToolCall.run_id == run_id).order_by(AgentToolCall.step_no, AgentToolCall.created_at)
                )
            ).scalars()
        )
        for r in rows:
            db.expunge(r)
        return rows


def outcomes(rows: list[AgentToolCall]) -> list[tuple[str, str, str | None]]:
    return [(c.tool_key, c.outcome, c.error_code) for c in rows]


async def proposals(tenant_id: uuid.UUID, run_id: uuid.UUID) -> list[AgentProposal]:
    async with tenant_session(tenant_id) as db:
        rows = list((await db.execute(select(AgentProposal).where(AgentProposal.run_id == run_id).order_by(AgentProposal.created_at))).scalars())
        for r in rows:
            db.expunge(r)
        return rows


async def decide(tenant_id: uuid.UUID, user_id: uuid.UUID, proposal_id: uuid.UUID, decision: str = "approve", **kw: Any) -> Any:
    from app.services import agent_runtime_service as runtime

    ctx = await ctx_for(tenant_id, user_id)
    async with tenant_session(tenant_id) as db:
        return await runtime.decide_proposal(ctx, db, proposal_id, ProposalDecisionIn(decision=decision, **kw))  # type: ignore[arg-type]


async def set_mode(tenant_id: uuid.UUID, agent_key: str, tool_key: str, mode: str) -> None:
    async with tenant_session(tenant_id) as db:
        existing = (
            await db.execute(select(AgentActionPolicy).where(AgentActionPolicy.agent_key == agent_key, AgentActionPolicy.tool_key == tool_key))
        ).scalar_one_or_none()
        if existing is None:
            db.add(AgentActionPolicy(id=uuid.uuid4(), tenant_id=tenant_id, agent_key=agent_key, tool_key=tool_key, mode=mode))
        else:
            await db.execute(update(AgentActionPolicy).where(AgentActionPolicy.id == existing.id).values(mode=mode))


__all__ = ["call", "calls", "ctx_for", "decide", "done", "outcomes", "proposals", "run_agent", "run_row", "set_mode", "turns"]
