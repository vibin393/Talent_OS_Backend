# ruff: file-ignore[line-too-long]
"""Autonomy policy resolution (Epic 53, AD-61, FR-268). ONE pure function -- no DB, no I/O -- decides every tool call.

Order (first match wins for ``off``): global kill -> tenant / platform kill -> features -> allow-list -> Effective Rights.
Reads always execute. Writes: ``base = action policy ?? tenant default ?? tool default ?? agent default``; burn-in
blocks ``auto``; the risk ceiling clamps (``high`` -> at most ``suggest``); untemplated egress, tainted egress,
suspicious input and Copilot writes are at most ``suggest``. ``auto`` therefore implies an effective risk of ``read``
or ``low`` whatever any DB row says.

Epic 66 (§0.15) branches, used only by the new origins (every existing origin resolves exactly as before):

* ``external`` (MCP; the run is always tainted): a tool without an MCP exposure, or whose group the tenant has not
  enabled -> ``off`` (``policy_violation``); a token lacking the exposure's ``mcp_scope`` or any of its
  ``resource_scopes`` -> ``off`` (``insufficient_scope``); reads execute; egress -> ``suggest`` (never auto); ``high``
  -> ``suggest``; ``low`` -> ``auto`` only when the tenant's action policy for (``mcp``, tool) is ``auto``.
* ``portal`` (portal-owned Copilot runs): reads execute; every write is a Draft (``suggest``).
"""

import dataclasses
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.core.agentic.exposure import ToolExposure
from app.core.agentic.types import RISK_CEILING, AutonomyMode, RiskClass, SendIntent, ToolSpec, min_mode
from app.core.permissions import Scope
from app.schemas.agentic import ModeDecision


@dataclasses.dataclass(frozen=True, slots=True)
class PolicyInput:
    global_kill: bool
    tenant_kill: bool
    agentic_on: bool
    agent_on: bool
    allow_list: frozenset[str]
    tool: ToolSpec
    args: Any | None
    effective_permissions: Mapping[str, Scope]
    action_policy_mode: AutonomyMode | None
    config_default_mode: AutonomyMode | None
    spec_default_mode: AutonomyMode
    origin: str
    run_tainted: bool = False
    suspicious: bool = False
    burn_in_until: datetime | None = None
    now: datetime | None = None
    send_intent: SendIntent | None = None
    grounded_ok: bool = False
    # Epic 66 (§0.15): origin "external" only -- the tool's exposure, the token's scopes, the tenant's MCP groups.
    exposure: ToolExposure | None = None
    token_scopes: frozenset[str] = frozenset()
    mcp_groups_enabled: frozenset[str] = frozenset()


def _off(reason: str, risk: str) -> ModeDecision:
    return ModeDecision(mode="off", risk=risk, reason=reason)


def resolve(inp: PolicyInput) -> ModeDecision:
    tool = inp.tool
    risk: RiskClass = tool.effective_risk(inp.args) if inp.args is not None else tool.risk
    if risk == "forbidden":  # pragma: no cover - register_tool refuses forbidden; defensive
        return _off("not_allowlisted", "high")
    if inp.global_kill:
        return _off("global_kill", risk)
    if inp.tenant_kill:
        return _off("kill_switch", risk)
    if not inp.agentic_on:
        return _off("feature_off", risk)
    if not inp.agent_on:
        return _off("agent_off", risk)
    if inp.origin == "external":
        refusal = _external_refusal(inp)
        if refusal is not None:
            return _off(refusal, risk)
    if tool.key not in inp.allow_list:
        return _off("not_allowlisted", risk)
    if not all(p in inp.effective_permissions for p in tool.permissions):
        return _off("no_permission", risk)
    if risk == "read":
        return ModeDecision(mode="auto", risk=risk, reason="policy")
    if inp.origin == "external":
        return _external_mode(inp, risk)
    base: AutonomyMode = (
        inp.action_policy_mode or inp.config_default_mode or tool.default_mode or inp.spec_default_mode
    )
    reason = "policy"
    if (
        base == "auto"
        and inp.burn_in_until is not None
        and inp.now is not None
        and inp.now < inp.burn_in_until
    ):
        base, reason = "suggest", "burn_in"
    ceiling = RISK_CEILING[risk]
    mode = min_mode(base, ceiling)
    if mode != base:
        reason = "ceiling"
    if mode == "auto" and tool.egress and inp.send_intent is not None:
        intent = inp.send_intent
        if not intent.templated and not intent.grounded:
            mode, reason = "suggest", "untemplated_send"
    if mode == "auto" and inp.run_tainted and tool.egress and not inp.grounded_ok:
        mode, reason = "suggest", "tainted_egress"
    if mode == "auto" and inp.suspicious:
        mode, reason = "suggest", "suspicious_input"
    if inp.origin == "copilot" and mode != "off":
        mode, reason = "suggest", "copilot_draft"
    if inp.origin == "portal" and mode != "off":
        mode, reason = "suggest", "portal_draft"
    return ModeDecision(mode=mode, risk=risk, reason=reason)


def _external_refusal(inp: PolicyInput) -> str | None:
    exp = inp.exposure
    if exp is None or exp.tool_key != inp.tool.key or "mcp" not in exp.surfaces:
        return "policy_violation"
    if exp.group not in inp.mcp_groups_enabled:
        return "policy_violation"
    if exp.mcp_scope is None or exp.mcp_scope not in inp.token_scopes:
        return "insufficient_scope"
    if not all(s in inp.token_scopes for s in exp.resource_scopes):
        return "insufficient_scope"
    return None


def _external_mode(inp: PolicyInput, risk: RiskClass) -> ModeDecision:
    """Resolve a non-read MCP call (the run is tainted by definition)."""
    if inp.tool.egress:
        return ModeDecision(mode="suggest", risk=risk, reason="tainted_egress")
    if risk == "high":
        return ModeDecision(mode="suggest", risk=risk, reason="ceiling")
    if inp.exposure is not None and inp.exposure.mcp_scope == "mcp:propose":  # Epic 73: always a Proposal
        return ModeDecision(mode="suggest", risk=risk, reason="ceiling")
    base: AutonomyMode = inp.action_policy_mode or "suggest"  # tenant opt-in per (mcp, tool); default suggest
    mode = min_mode(base, RISK_CEILING[risk])
    if mode == "auto" and inp.suspicious:
        return ModeDecision(mode="suggest", risk=risk, reason="suspicious_input")
    return ModeDecision(mode=mode, risk=risk, reason="policy")


__all__ = ["PolicyInput", "resolve"]
