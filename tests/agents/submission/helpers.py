# ruff: noqa: E501
"""Epic 61 agent test helpers: a shortlisted Application on the Epic 53 ``agent_org`` and a Fake-LLM run of the
``submission`` agent."""

import uuid
from typing import Any

from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.models.agent_runtime import AgentRun, AgentToolCall
from app.services import entity_refs
from app.services.ai.fake_llm import FakeTurn, fake_llm
from tests.modules.submission_packs.helpers import PackSeed

CONTEXT = "submissions.get_pack_context"
PREPARE = "submissions.prepare_pack"
WRITE = "submissions.save_write_up"
CONSENT = "submissions.request_consent"
CHECK = "submissions.check_pack"

WRITE_UP = (
    "Priya is a data engineer with five years of experience building lakehouse pipelines in PySpark at Infosys. "
    "She has shipped batch and streaming jobs on Databricks and owns data quality checks for a retail analytics "
    "platform. Inferred skills: Delta Lake (inferred). Notice period is 30 days. Based in Chennai, a fit for the "
    "hybrid role."
).replace("She has", "Priya has")


def turns(*raw: dict[str, Any]) -> list[FakeTurn]:
    return [FakeTurn.from_mapping(t) for t in raw]


def happy_script(seed: PackSeed, text: str = WRITE_UP) -> list[dict[str, Any]]:
    a = str(seed.application_id)
    return [
        {"kind": "tool_calls", "calls": [[CONTEXT, {"application_id": a}]]},
        {"kind": "tool_calls", "calls": [[PREPARE, {"application_id": a}]]},
        {"kind": "tool_calls", "calls": [[WRITE, {"application_id": a, "text": text}]]},
        {"kind": "emit_result", "result": {"outcome": "done", "summary": "Pack ready", "blockers": []}},
    ]


async def run_agent(seed: PackSeed, script: list[dict[str, Any]], *, owner: uuid.UUID | None = None) -> Any:
    from app.services import agent_runtime_service as runtime

    async with tenant_session(seed.tenant_id) as db:
        principal = await entity_refs.principal_for_user(db, seed.tenant_id, seed.admin.user_id)
    ctx = RequestContext(tenant_id=seed.tenant_id, principal=principal)
    with fake_llm(turns(*script)):
        async with tenant_session(seed.tenant_id) as db:
            ref = await runtime.start_run(
                ctx, db, agent_key="submission", trigger_kind="manual", trigger_key="manual",
                subject=("application", seed.application_id), owner_user_id=owner or seed.admin.user_id,
            )  # fmt: skip
    return ref


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
                    select(AgentToolCall)
                    .where(AgentToolCall.run_id == run_id)
                    .order_by(AgentToolCall.step_no, AgentToolCall.created_at)
                )
            ).scalars()
        )
        for r in rows:
            db.expunge(r)
        return rows


__all__ = [
    "CHECK",
    "CONSENT",
    "CONTEXT",
    "PREPARE",
    "WRITE",
    "WRITE_UP",
    "calls",
    "happy_script",
    "run_agent",
    "run_row",
    "turns",
]
