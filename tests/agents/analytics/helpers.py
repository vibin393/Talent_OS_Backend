"""Epic 64 test helpers: enable the analytics agent, run it under the Fake LLM, read answers / tool calls."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import Principal
from app.models.agent_runtime import AgentRun, AgentToolCall
from app.models.analytics_answers import AnalyticsAnswer
from app.services import agent_runtime_service as runtime
from app.services import ai
from app.services.ai.fake_llm import FakeTurn, fake_llm

SRC = "placements.placement"
FY_2026 = ["2026-04-01", "2027-03-31"]


async def enable(tenant_id: uuid.UUID, **features: bool) -> None:
    async with tenant_session(tenant_id) as db:
        await ai.set_features(db, tenant_id, {"agentic": True, "agent_analytics": True, **features})


async def user_ctx(tenant_id: uuid.UUID, user_id: uuid.UUID) -> RequestContext:
    from app.services import entity_refs

    async with tenant_session(tenant_id) as db:
        principal = await entity_refs.principal_for_user(db, tenant_id, user_id)
    assert isinstance(principal, Principal)
    return RequestContext(tenant_id=tenant_id, principal=principal)


def turns(script: Sequence[dict[str, Any]]) -> list[FakeTurn]:
    return [FakeTurn.from_mapping(t) for t in script]


async def run_agent(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    script: Sequence[dict[str, Any]],
    *,
    agent_key: str = "analytics",
    conversation_id: uuid.UUID | None = None,
) -> uuid.UUID:
    ctx = await user_ctx(tenant_id, user_id)
    with fake_llm(turns(script)):
        async with tenant_session(tenant_id) as db:
            ref = await runtime.start_run(
                ctx, db, agent_key=agent_key, trigger_kind="copilot", owner_user_id=user_id, origin="copilot",
                conversation_id=conversation_id,
            )  # fmt: skip
    assert ref.run_id is not None, ref.reason
    return ref.run_id


async def run_state(
    tenant_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[AgentRun, list[AgentToolCall], list[AnalyticsAnswer]]:
    async with tenant_session(tenant_id) as db:
        run = await db.get(AgentRun, run_id)
        assert run is not None
        calls = list(
            (
                await db.execute(
                    select(AgentToolCall)
                    .where(AgentToolCall.run_id == run_id)
                    .order_by(AgentToolCall.step_no, AgentToolCall.created_at)
                )
            ).scalars()
        )
        answers = list(
            (await db.execute(select(AnalyticsAnswer).where(AnalyticsAnswer.run_id == run_id))).scalars()
        )
        for obj in (run, *calls, *answers):
            db.expunge(obj)
    return run, calls, answers


def placements_by_client_fy(**over: Any) -> dict[str, Any]:
    return {
        "source": SRC,
        "filters": {"field": "planned_start_date", "op": "between", "value": FY_2026},
        "group_by": [{"field": "client.name"}, {"field": "planned_start_date", "bucket": "month"}],
        "measures": [
            {"key": "placements", "agg": "count"},
            {"key": "bill", "agg": "sum", "field": "bill_rate"},
        ],
        "sort": [{"key": "client.name"}],
        **over,
    }


def run_call(
    defn: dict[str, Any],
    viz: dict[str, Any] | None = None,
    question: str = "Placements by client this FY",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "kind": "tool_calls",
        "calls": [
            [
                "semantic.run",
                {
                    "definition": defn,
                    "viz": viz or {"type": "bar", "x": "client.name", "value": "placements"},
                    "question": question,
                    **extra,
                },
            ]
        ],
    }
