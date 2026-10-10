# ruff: noqa: E501
"""Epic 53 core.agentic registries (AD-59 / AD-72): tool refusal rules, agent placeholders, triggers, run limits,
Copilot capability gating."""

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.core.agentic import (
    FORBIDDEN_ACTIONS,
    AgentRegistryError,
    AgentSpec,
    CopilotCapability,
    RunLimits,
    ScopeRule,
    ToolRegistryError,
    ToolSpec,
    TriggerSpec,
    agents,
    get_tool_by_wire,
    register_agent,
    register_agent_trigger,
    register_tool,
)
from app.core.agentic.agents import unregister_agent
from app.core.agentic.copilot import (
    enabled_capabilities,
    register_copilot_capability,
    unregister_copilot_capability,
)
from app.core.agentic.tools import unregister_tool
from app.core.hooks import autodiscover
from app.models.ai import AGENT_KEYS


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: int = 0


class Loose(BaseModel):
    x: int = 0


class Reserved(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = ""


async def _h(tctx: Any, args: Any) -> Any:  # pragma: no cover
    return None


async def _p(tctx: Any, args: Any) -> Any:  # pragma: no cover
    return None


def spec(**kw: Any) -> ToolSpec:
    base: dict[str, Any] = {
        "key": "zz.test_tool", "version": 1, "owner_epic": "53", "title": "t", "description": "does x, not y",
        "input_model": In, "output_model": In, "permissions": (), "risk": "read", "scope": ScopeRule("none"), "handler": _h,
    }  # fmt: skip
    base.update(kw)
    return ToolSpec(**base)


@pytest.mark.parametrize(
    "kw",
    [
        {"key": "BadKey"},
        {"key": "nodot"},
        {"risk": "forbidden"},
        {"input_model": Loose},
        {"input_model": Reserved},
        {"risk": "low"},  # no preview
        {"risk": "read", "egress": True, "send_intent": _p},
        {"risk": "low", "preview": _p, "egress": True},  # no send_intent
        {"description": ""},
    ],
)
def test_register_tool_refusals(kw: dict[str, Any]) -> None:
    with pytest.raises(ToolRegistryError):
        register_tool(spec(**kw))


def test_register_duplicate_and_wire_name() -> None:
    s = spec()
    register_tool(s)
    try:
        assert s.wire_name == "zz__test_tool"
        assert get_tool_by_wire("zz__test_tool") is s
        with pytest.raises(ToolRegistryError):
            register_tool(s)
    finally:
        unregister_tool(s.key)


def test_placeholders_cover_the_fifteen_keys() -> None:
    autodiscover("app.services")
    registered = agents()
    assert set(AGENT_KEYS) <= set(registered)
    assert len(AGENT_KEYS) == 20  # 15 Phase 4 keys + Epic 66: mcp, copilot_client|vendor|candidate, campaign_copy
    # Phase 4 owners replaced their placeholders; the Phase 5 keys stay placeholders until 73 / 77 / 78.
    p5 = ("mcp", "copilot_client", "copilot_vendor", "copilot_candidate", "campaign_copy")
    assert all(registered[k].placeholder for k in p5)


def test_register_replaces_a_placeholder_once() -> None:
    autodiscover("app.services")
    original = agents()["campaign_copy"]  # a placeholder (Epic 78 fills it)
    real = AgentSpec(
        key="campaign_copy",
        version=1,
        owner_epic="78",
        name="Campaign copy",
        purpose="p",
        prompt_id="agent_campaign_copy",
    )
    try:
        register_agent(real)
        assert agents()["campaign_copy"] is real
        register_agent(original)  # a placeholder never replaces a real definition
        assert agents()["campaign_copy"] is real
        with pytest.raises(AgentRegistryError):
            register_agent(real)
    finally:
        unregister_agent("campaign_copy")
        register_agent(original)


def test_triggers_and_limits() -> None:
    spec_ = AgentSpec(key="_trig", version=1, owner_epic="53", name="T", purpose="p", prompt_id="agent__trig")
    register_agent(spec_)
    try:
        register_agent_trigger(
            "_trig", TriggerSpec(key="on_created", kind="event", event="ApplicationCreated")
        )
        with pytest.raises(AgentRegistryError):
            register_agent_trigger("_trig", TriggerSpec(key="on_created", kind="event", event="X"))
        with pytest.raises(AgentRegistryError):
            register_agent_trigger("_trig", TriggerSpec(key="nightly", kind="schedule", schedule="bad"))
    finally:
        unregister_agent("_trig")
    limits = RunLimits()
    assert limits.narrowed({"max_steps": 5}).max_steps == 5
    for bad in ({"max_steps": 13}, {"max_steps": 0}, {"nope": 1}):
        with pytest.raises(ValueError, match=r"."):
            limits.narrowed(bad)


def test_copilot_capability_gating() -> None:
    cap = CopilotCapability(
        key="zz_cap",
        owner_epic="64",
        title="t",
        description="d",
        tools=(),
        agent_key="analytics",
        permission="report:read",
    )
    register_copilot_capability(cap)
    try:
        assert cap in enabled_capabilities({"agent_analytics": True}, lambda p: p == "report:read")
        assert cap not in enabled_capabilities({"agent_analytics": False}, lambda p: True)
        assert cap not in enabled_capabilities({"agent_analytics": True}, lambda p: False)
    finally:
        unregister_copilot_capability("zz_cap")


def test_forbidden_actions_documented() -> None:
    assert ("application", "rejected") in FORBIDDEN_ACTIONS
    assert ("offer", "released") in FORBIDDEN_ACTIONS
