# ruff: noqa: E501
"""Epic 53 gateway step (AD-57, 53.4): pre-check order and codes, one ai_actions row per call, request shape,
redaction, stop-reason mapping, tiers, pricing, Fake LLM behaviour and settings guards."""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.settings import Settings, get_settings
from app.models.ai import AIAction, AITenantSettings
from app.schemas.agentic import ModelToolDef, StepRequest, StepUsage
from app.services import ai
from app.services.ai.fake_llm import FakeLLM, FakeTurn, fake_llm, load_script
from tests.agents.conftest import AgentOrg, enable


def req(**kw: Any) -> StepRequest:
    base: dict[str, Any] = {
        "agent_key": "_test_agent", "run_id": uuid.uuid4(), "step_no": 1, "model_tier": "standard", "effort": "medium",
        "system": "sys", "tools": (ModelToolDef(name="notes__add", description="d", input_schema={"type": "object"}),),
        "messages": ({"role": "user", "content": [{"type": "text", "text": "PAN ABCPE1234F please"}]},),
    }  # fmt: skip
    base.update(kw)
    return StepRequest(**base)


async def step(org: AgentOrg, r: StepRequest) -> tuple[Any, AIAction]:
    ctx = RequestContext(tenant_id=org.tenant_id)
    async with tenant_session(org.tenant_id) as db:
        result = await ai.run_agent_step(ctx, db, r)
        row = await db.get(AIAction, result.action_id)
        assert row is not None
        db.expunge(row)
    return result, row


async def test_ok_step_records_one_action_and_request_shape(agent_org: AgentOrg) -> None:
    with fake_llm([FakeTurn("tool_calls", calls=(("notes.add", {"x": 1}),), usage=(1000, 200))]) as fake:
        result, row = await step(agent_org, req())
    assert result.ok and result.stop_reason == "tool_use"
    assert [t.name for t in result.tool_uses] == ["notes__add"]
    (call,) = fake.calls
    assert call["tool_choice"] == {"type": "auto"} and call["thinking"] == {"type": "adaptive"}
    assert call["model"] == "claude-sonnet-5-5"
    assert call["messages"][0]["content"][0]["text"] == "PAN [REDACTED] please"
    assert call["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert (row.kind, row.status, row.agent_key, row.step_no, row.model) == (
        "agent_step",
        "ok",
        "_test_agent",
        1,
        "fake",
    )
    assert row.feature == "agent__test_agent"
    assert row.output == {"stop_reason": "tool_use", "tool_names": ["notes__add"]}
    assert (row.input_tokens, row.output_tokens) == (1000, 200)


@pytest.mark.parametrize(
    ("turn", "code"),
    [
        (FakeTurn("refusal", refusal_category="cyber"), "refused"),
        (FakeTurn("truncated"), "truncated"),
        (FakeTurn("error", error="timeout"), "timeout"),
        (FakeTurn("error", error="provider_error"), "provider_error"),
    ],
)
async def test_failure_mapping(agent_org: AgentOrg, turn: FakeTurn, code: str) -> None:
    with fake_llm([turn]):
        result, row = await step(agent_org, req())
    assert (result.ok, result.error_code) == (False, code)
    assert row.error_code == code
    if code == "refused":
        assert result.refusal_category == "cyber"


async def test_pause_is_ok(agent_org: AgentOrg) -> None:
    with fake_llm([FakeTurn("pause")]):
        result, _ = await step(agent_org, req())
    assert result.ok and result.stop_reason == "pause_turn"


async def test_precheck_order(agent_org: AgentOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    with fake_llm([]) as fake:
        async with tenant_session(agent_org.tenant_id) as db:
            await db.execute(
                update(AITenantSettings).values(
                    kill_switch_at=__import__("datetime").datetime.now(__import__("datetime").UTC)
                )
            )
        result, _ = await step(agent_org, req())
        assert result.error_code == "kill_switch"
        monkeypatch.setattr(get_settings(), "agents_globally_disabled", True)
        result, _ = await step(agent_org, req())
        assert result.error_code == "global_kill"
        monkeypatch.setattr(get_settings(), "agents_globally_disabled", False)
        async with tenant_session(agent_org.tenant_id) as db:
            await db.execute(update(AITenantSettings).values(kill_switch_at=None))
        await enable(agent_org.tenant_id, agentic=False)
        result, _ = await step(agent_org, req())
        assert result.error_code == "feature_disabled"
        await enable(agent_org.tenant_id)
        result, _ = await step(agent_org, req(agent_key="demand"))
        assert result.error_code == "feature_disabled"  # agent_demand off
    assert fake.calls == []


async def test_not_configured_in_production(agent_org: AgentOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "ai_fake_when_keyless", False)
    result, _ = await step(agent_org, req())
    assert result.error_code == "not_configured"


async def test_keyless_fake_and_deep_tier_without_opt_in(agent_org: AgentOrg) -> None:
    result, row = await step(agent_org, req(model_tier="deep"))  # keyless non-prod -> default.yaml fake
    assert result.ok and row.model == "fake"
    with fake_llm([FakeTurn("text", text="hi")]) as fake:
        await step(agent_org, req(model_tier="deep"))
    assert fake.calls[0]["model"] == "claude-sonnet-5-5"
    assert ai.model_for("deep", deep_opt_in=True) == "claude-opus-5-5"
    assert ai.model_for("fast", deep_opt_in=False) == "claude-haiku-5-5"


def test_pricing() -> None:
    # micro-USD: 1 token at $1/MTok = 1 micro-USD
    assert (
        ai.cost_micros("claude-sonnet-5-5", StepUsage(input_tokens=1_000_000, output_tokens=0)) == 2_000_000
    )
    assert ai.cost_micros("claude-sonnet-5-5", StepUsage(output_tokens=100)) == 1000
    assert ai.cost_micros("unknown-model", StepUsage(input_tokens=10)) == 0


def test_fake_provider_refused_in_production() -> None:
    with pytest.raises(ValidationError):
        Settings(
            app_env="production",
            ai_provider="fake",
            db_app_password="x",
            audit_master_key="k",
            mfa_encryption_key="k",
        )


def test_fake_llm_script_yaml_and_exhaustion(tmp_path: Any) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(
        "- kind: tool_calls\n  calls: [[notes.add, {x: 1}]]\n- kind: text\n  text: done\n", encoding="utf-8"
    )
    turns = load_script(path)
    assert isinstance(turns, list) and turns[0].calls == (("notes.add", {"x": 1}),)
    fake = FakeLLM.from_yaml(path)
    from app.services.ai.fake_llm import build_message

    _ = build_message
    assert len(turns) == 2
    _ = fake
    from app.services.ai.fake_llm import EXHAUSTED_RESULT, _Counter

    msg = build_message(FakeTurn("emit_result"), _Counter())
    assert msg.content[0].name == "emit_result" and msg.content[0].input == EXHAUSTED_RESULT


async def test_stream_step_forwards_text(agent_org: AgentOrg) -> None:
    seen: list[str] = []

    async def sink(kind: str, data: Any) -> None:
        seen.append(data["text"])

    with fake_llm([FakeTurn("text", text="hello there world")]):
        async with tenant_session(agent_org.tenant_id) as db:
            result = await ai.stream_agent_step(
                RequestContext(tenant_id=agent_org.tenant_id), db, req(), sink
            )
    assert result.ok and "".join(seen) == "hello there world"


async def test_expect_failure_is_provider_error(agent_org: AgentOrg) -> None:
    with fake_llm([FakeTurn("text", text="x", expect=lambda m: False)]) as fake:
        result, _ = await step(agent_org, req())
    assert result.error_code == "provider_error" and fake.failures
    async with tenant_session(agent_org.tenant_id) as db:
        assert (
            await db.execute(select(AIAction).where(AIAction.agent_key == "_test_agent"))
        ).first() is not None
