"""Epic 53 executor (AD-58): steps, tool calls, proposals, caps, policy violations, idempotency, history."""

import itertools
import uuid
from typing import Any

from sqlalchemy import func, select

from app.core.database import tenant_session
from app.models.agent_runtime import AgentProposal, AgentRun, AgentRunStep, AgentToolCall
from app.models.ai import AIAction
from app.schemas.agentic import RunRef
from app.services import agent_runtime_service as svc
from app.services.agent_runtime import executor, payloads
from app.services.ai.fake_llm import FakeTurn, fake_llm
from tests.agents.conftest import AgentOrg


async def start(org: AgentOrg, agent_key: str = "_test_agent", **kw: Any) -> RunRef:
    kw.setdefault("subject", ("candidate", org.candidate_id))
    kw.setdefault("owner_user_id", org.admin.user_id)
    async with tenant_session(org.tenant_id) as db:
        return await svc.start_run(org.ctx(), db, agent_key=agent_key, trigger_kind="manual", **kw)


async def run_of(org: AgentOrg, run_id: uuid.UUID) -> AgentRun:
    async with tenant_session(org.tenant_id) as db:
        run = await db.get(AgentRun, run_id)
        assert run is not None
        db.expunge(run)
        return run


async def calls_of(org: AgentOrg, run_id: uuid.UUID) -> list[AgentToolCall]:
    async with tenant_session(org.tenant_id) as db:
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


async def test_happy_path_read_write_and_proposal(agent_org: AgentOrg) -> None:
    cid = str(agent_org.candidate_id)
    script = [
        FakeTurn(
            "tool_calls", calls=(("consent.status", {"candidate_id": cid, "purposes": ["ai_processing"]}),)
        ),
        FakeTurn(
            "tool_calls",
            calls=(
                (
                    "notes.add",
                    {"entity_type": "candidate", "entity_id": cid, "body": "Looks strong on Python."},
                ),
                (
                    "messaging.send",
                    {
                        "recipient_kind": "candidate",
                        "recipient_id": cid,
                        "channel": "email",
                        "subject": "Hi",
                        "body": "Hello Priya",
                    },
                ),
            ),
        ),
        FakeTurn("emit_result", result={"outcome": "done", "summary": "Reviewed."}),
    ]
    with fake_llm(script) as fake:
        ref = await start(agent_org)
    assert ref.run_id is not None, ref
    run = await run_of(agent_org, ref.run_id)
    assert run.status == "completed", (run.status, run.reason_code)
    assert run.result == {"outcome": "done", "summary": "Reviewed."}
    assert len(fake.calls) == 3
    first = fake.calls[0]
    assert first["tool_choice"] == {"type": "auto"}
    assert first["thinking"] == {"type": "adaptive"}
    assert first["output_config"] == {"effort": "medium"}
    assert all(t["strict"] is True for t in first["tools"])
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    names = [t["name"] for t in first["tools"]]
    assert names == sorted(names) and "emit_result" in names and "messaging__send" in names
    calls = await calls_of(agent_org, ref.run_id)
    outcomes = {c.tool_key: c.outcome for c in calls}
    assert outcomes == {"consent.status": "executed", "notes.add": "executed", "messaging.send": "proposed"}
    async with tenant_session(agent_org.tenant_id) as db:
        steps = (
            await db.execute(
                select(func.count()).select_from(AgentRunStep).where(AgentRunStep.run_id == ref.run_id)
            )
        ).scalar_one()
        actions = list(
            (await db.execute(select(AIAction).where(AIAction.agent_run_id == ref.run_id))).scalars()
        )
        proposal = (
            await db.execute(select(AgentProposal).where(AgentProposal.run_id == ref.run_id))
        ).scalar_one()
    assert steps == 3
    assert len(actions) == 3 and all(a.kind == "agent_step" and a.model == "fake" for a in actions)
    assert {a.step_no for a in actions} == {1, 2, 3}
    assert proposal.status == "pending" and proposal.risk == "high" and proposal.tool_key == "messaging.send"
    assert proposal.assignee_user_id == agent_org.admin.user_id


async def test_history_is_append_only(agent_org: AgentOrg) -> None:
    cid = str(agent_org.candidate_id)
    script = [
        FakeTurn("tool_calls", calls=(("consent.status", {"candidate_id": cid}),)),
        FakeTurn("tool_calls", calls=(("consent.status", {"candidate_id": cid, "purposes": ["marketing"]}),)),
        FakeTurn("emit_result", result={"outcome": "done", "summary": "ok"}),
    ]
    with fake_llm(script) as fake:
        ref = await start(agent_org)
    msgs = [c["messages"] for c in fake.calls]
    for earlier, later in itertools.pairwise(msgs):
        prefix = later[: len(earlier)]

        # The last block of the newest message carries cache_control; compare without it.
        def strip(m: list[dict[str, Any]]) -> list[Any]:
            return [
                {**x, "content": [{k: v for k, v in b.items() if k != "cache_control"} for b in x["content"]]}
                if isinstance(x["content"], list)
                else x
                for x in m
            ]

        assert strip(prefix) == strip(earlier)
    assert ref.run_id is not None
    async with tenant_session(agent_org.tenant_id) as db:
        history = await payloads.history(db, ref.run_id, before_step=3)
    assert len(history) == 5  # kickoff, assistant, tool results, assistant, tool results


async def test_not_allowlisted_tool_is_policy_violation(agent_org: AgentOrg) -> None:
    script = [FakeTurn("tool_calls", calls=(("offers__release", {"offer_id": str(uuid.uuid4())}),))] * 3
    with fake_llm(script):
        ref = await start(agent_org)
    assert ref.run_id is not None
    run = await run_of(agent_org, ref.run_id)
    assert run.status == "failed" and run.reason_code == "policy_violation"
    assert run.policy_violations == 3
    assert run.fallback_task_id is not None
    calls = await calls_of(agent_org, ref.run_id)
    assert {c.outcome for c in calls} == {"policy_violation"}


async def test_invalid_args_and_subject_binding(agent_org: AgentOrg, other_org: AgentOrg) -> None:
    stranger = str(uuid.uuid4())
    script = [
        FakeTurn(
            "tool_calls",
            calls=(("notes.add", {"entity_type": "candidate", "entity_id": "nope", "body": "x"}),),
        ),
        FakeTurn(
            "tool_calls",
            calls=(("notes.add", {"entity_type": "candidate", "entity_id": stranger, "body": "x"}),),
        ),
        FakeTurn("emit_result", result={"outcome": "done", "summary": "ok"}),
    ]
    with fake_llm(script):
        ref = await start(agent_org)
    assert ref.run_id is not None
    calls = await calls_of(agent_org, ref.run_id)
    assert [c.outcome for c in calls] == ["invalid", "not_found"]


async def test_step_cap_stops_with_fallback(agent_org: AgentOrg) -> None:
    from app.core.agentic import RunLimits, get_agent

    cid = str(agent_org.candidate_id)
    script = [FakeTurn("tool_calls", calls=(("consent.status", {"candidate_id": cid}),))] * 20
    spec = get_agent("_test_agent")
    assert spec.limits.max_steps == RunLimits().max_steps
    with fake_llm(script) as fake:
        ref = await start(agent_org)
    assert ref.run_id is not None
    run = await run_of(agent_org, ref.run_id)
    assert (run.status, run.reason_code) == ("stopped", "cap_steps")
    assert run.step_count == 12 and len(fake.calls) == 12
    assert run.fallback_task_id is not None
    # 80 % warning appended as a mid-run system notice
    later = fake.calls[-1]["messages"]
    assert any("system_notice" in str(m["content"]) for m in later)


async def test_replayed_step_is_a_noop(agent_org: AgentOrg) -> None:
    cid = str(agent_org.candidate_id)
    script = [
        FakeTurn(
            "tool_calls",
            calls=(("notes.add", {"entity_type": "candidate", "entity_id": cid, "body": "note"}),),
        ),
        FakeTurn("emit_result", result={"outcome": "done", "summary": "ok"}),
    ]
    with fake_llm(script) as fake:
        ref = await start(agent_org)
        assert ref.run_id is not None
        again = await executor.run_step(agent_org.tenant_id, ref.run_id, 1, worker_id="w2")
    assert again.note == "not_claimable"
    assert len(fake.calls) == 2
    calls = await calls_of(agent_org, ref.run_id)
    assert [c.tool_key for c in calls] == ["notes.add"]


def placeholder_key() -> str:
    """A test-only disabled placeholder (every shipped placeholder is being replaced by its owner epic)."""
    from app.core.agentic import AgentSpec, agents, register_agent

    if "_test_placeholder" not in agents():
        register_agent(
            AgentSpec(
                key="_test_placeholder", version=1, owner_epic="53", name="Test Placeholder",
                purpose="Placeholder tests", prompt_id="agent__test_placeholder", placeholder=True,
            )
        )  # fmt: skip
    return "_test_placeholder"


async def test_feature_off_and_placeholder_do_not_start(agent_org: AgentOrg) -> None:
    from tests.agents.conftest import enable

    async with tenant_session(agent_org.tenant_id) as db:
        ref = await svc.start_run(
            agent_org.ctx(),
            db,
            agent_key=placeholder_key(),  # Epic 63 replaced the last shipped placeholder used here
            trigger_kind="manual",
            owner_user_id=agent_org.admin.user_id,
        )
    assert (ref.status, ref.reason) == ("not_started", "agent_not_available")
    await enable(agent_org.tenant_id, agentic=False)
    ref = await start(agent_org)
    assert (ref.run_id, ref.reason) == (None, "feature_off")


async def test_dedup_returns_active_run(agent_org: AgentOrg) -> None:
    script = [FakeTurn("emit_result", result={"outcome": "done", "summary": "ok"})]
    from app.core.agentic import TriggerSpec

    _ = TriggerSpec
    async with tenant_session(agent_org.tenant_id) as db:
        with fake_llm(script):
            first = await svc.start_run(
                agent_org.ctx(),
                db,
                agent_key="_test_agent",
                trigger_kind="manual",
                owner_user_id=agent_org.admin.user_id,
                dedup_key="k1",
            )
            second = await svc.start_run(
                agent_org.ctx(),
                db,
                agent_key="_test_agent",
                trigger_kind="manual",
                owner_user_id=agent_org.admin.user_id,
                dedup_key="k1",
            )
    assert first.run_id == second.run_id and second.reason == "deduplicated"


async def test_no_owner_creates_task_not_run(agent_org: AgentOrg) -> None:
    async with tenant_session(agent_org.tenant_id) as db:
        ref = await svc.start_run(
            agent_org.ctx(),
            db,
            agent_key="_test_agent",
            trigger_kind="event",
            subject=("candidate", agent_org.candidate_id),
        )
    assert (ref.run_id, ref.reason) == (None, "owner_unavailable")
