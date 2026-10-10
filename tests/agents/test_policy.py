# ruff: noqa: E501
"""Epic 53 autonomy policy (AD-61, FR-268): every branch + property tests (auto => risk in {read, low})."""

from datetime import UTC, datetime, timedelta
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict

from app.core.agentic.types import ProposalPreview, Recipient, ScopeRule, SendIntent, ToolOutcome, ToolSpec
from app.core.permissions import Scope
from app.services.agent_runtime.policy import PolicyInput, resolve


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: int = 0


async def _handler(tctx: Any, args: Any) -> ToolOutcome:  # pragma: no cover - never called
    return ToolOutcome(_In())


async def _preview(tctx: Any, args: Any) -> ProposalPreview:  # pragma: no cover
    return ProposalPreview("t", "s")


async def _intent(tctx: Any, args: Any) -> SendIntent:  # pragma: no cover
    return SendIntent((), True, None)


def tool(
    risk: str = "low", *, egress: bool = False, risk_fn: Any = None, default_mode: Any = None
) -> ToolSpec:
    return ToolSpec(
        key="test.tool", version=1, owner_epic="53", title="t", description="d", input_model=_In, output_model=_In,
        permissions=("note:write",), risk=risk, scope=ScopeRule("none"), handler=_handler, preview=_preview,  # type: ignore[arg-type]
        egress=egress, send_intent=_intent if egress else None, risk_fn=risk_fn, default_mode=default_mode,
    )  # fmt: skip


def inp(t: ToolSpec, **kw: Any) -> PolicyInput:
    base: dict[str, Any] = {
        "global_kill": False, "tenant_kill": False, "agentic_on": True, "agent_on": True,
        "allow_list": frozenset({t.key}), "tool": t, "args": None, "effective_permissions": {"note:write": Scope.TENANT},
        "action_policy_mode": None, "config_default_mode": None, "spec_default_mode": "auto", "origin": "agent",
    }  # fmt: skip
    base.update(kw)
    return PolicyInput(**base)


def test_off_branches_in_order() -> None:
    t = tool()
    assert resolve(inp(t, global_kill=True, tenant_kill=True)).reason == "global_kill"
    assert resolve(inp(t, tenant_kill=True)).reason == "kill_switch"
    assert resolve(inp(t, agentic_on=False)).reason == "feature_off"
    assert resolve(inp(t, agent_on=False)).reason == "agent_off"
    assert resolve(inp(t, allow_list=frozenset())).reason == "not_allowlisted"
    assert resolve(inp(t, effective_permissions={})).reason == "no_permission"
    for kw in ({"global_kill": True}, {"allow_list": frozenset()}, {"effective_permissions": {}}):
        assert resolve(inp(t, **kw)).mode == "off"


def test_mode_precedence_and_ceiling() -> None:
    t = tool("low", default_mode="suggest")
    assert resolve(inp(t)).mode == "suggest"  # tool default beats spec default
    assert resolve(inp(t, config_default_mode="auto")).mode == "auto"
    assert resolve(inp(t, config_default_mode="auto", action_policy_mode="off")).mode == "off"
    high = tool("high")
    decision = resolve(inp(high, action_policy_mode="auto"))  # a tampered DB row
    assert (decision.mode, decision.reason) == ("suggest", "ceiling")
    assert resolve(inp(tool("read"), action_policy_mode="off")).mode == "auto"  # reads always execute


def test_risk_fn_only_raises() -> None:
    t = tool("low", risk_fn=lambda a: "high" if a.x else "read")
    assert resolve(inp(t, args=_In(x=0))).risk == "low"
    assert resolve(inp(t, args=_In(x=1))).mode == "suggest"


def test_burn_in_tainted_untemplated_suspicious_copilot() -> None:
    now = datetime.now(UTC)
    t = tool()
    assert resolve(inp(t, burn_in_until=now + timedelta(days=1), now=now)).reason == "burn_in"
    egress = tool(egress=True)
    templated = SendIntent((Recipient("candidate", __import__("uuid").uuid4(), "email"),), True, "t")
    assert resolve(inp(egress, send_intent=templated)).mode == "auto"
    assert resolve(inp(egress, send_intent=SendIntent((), False, None))).reason == "untemplated_send"
    assert resolve(inp(egress, send_intent=templated, run_tainted=True)).reason == "tainted_egress"
    assert resolve(inp(egress, send_intent=templated, run_tainted=True, grounded_ok=True)).mode == "auto"
    assert resolve(inp(t, suspicious=True)).reason == "suspicious_input"
    assert resolve(inp(t, origin="copilot")).reason == "copilot_draft"
    assert resolve(inp(tool("read"), origin="copilot")).mode == "auto"


MODES = st.sampled_from(["off", "suggest", "auto", None])


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    risk=st.sampled_from(["read", "low", "high"]),
    raised=st.booleans(),
    policy=MODES,
    config=MODES,
    tool_default=MODES,
    spec_default=st.sampled_from(["off", "suggest", "auto"]),
    egress=st.booleans(),
    templated=st.booleans(),
    tainted=st.booleans(),
    copilot=st.booleans(),
    burn_in=st.booleans(),
    suspicious=st.booleans(),
)
def test_property_auto_implies_low_risk(
    risk: str,
    raised: bool,
    policy: Any,
    config: Any,
    tool_default: Any,
    spec_default: str,
    egress: bool,
    templated: bool,
    tainted: bool,
    copilot: bool,
    burn_in: bool,
    suspicious: bool,
) -> None:
    t = tool(
        risk,
        egress=egress and risk != "read",
        risk_fn=(lambda a: "high") if raised else None,
        default_mode=tool_default,
    )
    now = datetime.now(UTC)
    decision = resolve(
        inp(
            t,
            args=_In(),
            action_policy_mode=policy,
            config_default_mode=config,
            spec_default_mode=spec_default,
            send_intent=SendIntent((), templated, None) if t.egress else None,
            run_tainted=tainted,
            origin="copilot" if copilot else "agent",
            burn_in_until=now + timedelta(days=1) if burn_in else None,
            now=now,
            suspicious=suspicious,
        )
    )
    if decision.mode == "auto":
        assert decision.risk in ("read", "low")
        if decision.risk != "read":
            assert not copilot and not burn_in and not suspicious
            if t.egress:
                assert templated and not tainted
    if raised:
        assert decision.mode != "auto"
