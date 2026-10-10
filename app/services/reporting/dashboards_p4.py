# ruff: noqa: E501, N812, RUF001
"""Phase 4 dashboards (Epic 65 A4, FR-321, NFR-53): "AI agents" (operations and quality, ``ai:govern``) and "Agent
outcomes" (SM-30..SM-39, SM-C14..SM-C18, ``report:read``).

Merged into the P1 ``DASHBOARDS`` registry with the same tile mechanics (independent tiles, Redis cache, the P2
statement timeout, ``timeout`` / ``unavailable``, drill-down links) and the ``?agent_key=`` filter. Tiles live in the
``P4_TILES`` registry (``_p4``): adding an agent-specific tile later is a registry entry, not a new dashboard. No tile
shows Proposal content, conversation text, self-ID or fairness data.
"""

from typing import Any

from app.core.permissions import Principal
from app.services.reporting import p4tables as _P4  # noqa: F401 - registers the optional-table guards
from app.services.reporting.dashboards import DashboardDef, TileDef, TileFn
from app.services.reporting.metrics import agent_outcomes as _O
from app.services.reporting.metrics import agents as _A
from app.services.reporting.p2tables import needs as _need

P4_TILES: dict[str, TileDef] = {}


def _p4(
    key: str, title: str, kind: str, size: str, desc: str, fn: TileFn, *, gate: tuple[str, ...], **kw: Any
) -> TileDef:
    tile = TileDef(key, title, kind, size, desc, fn, permissions=gate, **kw)
    P4_TILES[key] = tile
    return tile


GOVERN = ("ai:govern",)
REPORT = ("report:read",)

_AGENT_TILES = (
    _p4("agent_runs_by_status", "Agent runs by status", "breakdown", "m",
        "Agent runs created in range, by status.", _A.agent_runs_by_status, gate=GOVERN, requires=_need("agent_runs")),
    _p4("agent_success_rates", "Success rates", "kpi", "s",
        "Completed ÷ finished runs; failure and fallback-task shares per agent.", _A.agent_success_rates, gate=GOVERN,
        requires=_need("agent_runs")),
    _p4("agent_scorecard", "Agent scorecard", "table", "l",
        "Per agent: runs, success, failures, fallbacks, Proposals, acceptance / edit / rejection / expiry, median time to decision, auto actions, tokens and cost (USD).",
        _A.agent_scorecard, gate=GOVERN, requires=_need("agent_runs", "agent_proposals", "agent_tool_calls")),
    _p4("proposal_outcomes", "Proposal outcomes", "breakdown", "m",
        "Agent Proposals created in range: approved, edited, rejected, expired, stale and execution failed.",
        _A.proposal_outcomes, gate=GOVERN, requires=_need("agent_proposals")),
    _p4("time_to_decision", "Time to decision", "kpi", "s",
        "Median and 90th percentile hours from a Proposal to its decision.", _A.time_to_decision, gate=GOVERN,
        requires=_need("agent_proposals")),
    _p4("agent_tokens_cost", "Tokens and cost", "series", "l",
        "Agent model cost (USD, estimates from ai_prices) and tokens per day and agent.", _A.agent_tokens_cost, gate=GOVERN),
    _p4("copilot_ratings", "Copilot ratings", "kpi", "s",
        "Share of Copilot answers rated helpful, and the number of ratings.", _A.copilot_ratings, gate=GOVERN,
        requires=_need("copilot_feedback")),
    _p4("policy_violations_7d", "Policy violations (7 days)", "kpi", "s",
        "Refused tool calls and runs stopped for a policy violation in the last 7 days.", _A.policy_violations_7d,
        gate=GOVERN, requires=_need("agent_runs", "agent_tool_calls")),
    _p4("top_failure_reasons", "Top failure reasons", "table", "m",
        "Failed and stopped runs in range by reason and agent.", _A.top_failure_reasons, gate=GOVERN,
        requires=_need("agent_runs")),
    _p4("budget_usage", "Budget usage", "table", "m",
        "Per agent: monthly token budget, tokens used this month and % used.", _A.budget_usage, gate=GOVERN,
        requires=_need("agent_configs")),
)  # fmt: skip

_OUTCOME_TILES = (
    _p4("sm30_coordination_time", "Estimated hours saved per recruiter per week", "kpi", "m",
        "Estimate (SM-30): executed automatic actions and approved Proposals × minutes saved per action, per active recruiter and week.",
        _O.sm30_coordination_time, gate=REPORT, estimate=True, requires=_need("agent_runs", "agent_tool_calls", "agent_proposals")),
    _p4("sm31_time_to_first_submission", "Time to first submission", "kpi", "s",
        "SM-31: median business days from a Demand opening to its first Submission (last 90 days) vs the 90 days before AI agents were turned on (target −20%).",
        _O.sm31_time_to_first_submission, gate=(*REPORT, "demand:read")),
    _p4("sm32_proposal_acceptance", "Proposal acceptance", "table", "m",
        "SM-32: approved ÷ decided Proposals per agent (target ≥ 70%).", _O.sm32_proposal_acceptance, gate=REPORT,
        requires=_need("agent_proposals")),
    _p4("sm33_scheduling_cycle_time", "Scheduling cycle time", "kpi", "s",
        "SM-33: median hours from a scheduling request to the booked interview (target ≤ 24).",
        _O.sm33_scheduling_cycle_time, gate=REPORT, requires=_need("scheduling_requests")),
    _p4("sm34_screening_completion", "Screening completion", "kpi", "s",
        "SM-34: share of invited text and voice screenings completed within 72 hours (target ≥ 60%).",
        _O.sm34_screening_completion, gate=REPORT, requires=_need("screening_conversations")),
    _p4("sm35_copilot_adoption", "Copilot adoption", "kpi", "s",
        "SM-35: weekly active Copilot users ÷ active users (target ≥ 50%) and helpful ratings (target ≥ 80%).",
        _O.sm35_copilot_adoption, gate=REPORT, requires=_need("copilot_messages", "copilot_feedback")),
    _p4("sm36_prestart_compliance", "Pre-start compliance", "kpi", "s",
        "SM-36: Placements started with every required checklist item complete 2 days before start.",
        _O.sm36_prestart_compliance, gate=(*REPORT, "placement:read")),
    _p4("sm37_time_and_cash", "Timesheets on time and DSO", "kpi", "s",
        "SM-37: on-time Timesheet submission share and days sales outstanding.", _O.sm37_time_and_cash,
        gate=REPORT, requires=_need("timesheets")),
    _p4("sm38_analytics_self_service", "Analytics self-service", "kpi", "s",
        "SM-38: share of analytics answers saved as reports, and the latest analytics evaluation score.",
        _O.sm38_analytics_self_service, gate=REPORT, requires=_need("analytics_answers")),
    _p4("sm39_governance_integrity", "Governance integrity", "kpi", "s",
        "SM-39: ★ transitions by non-human actors (target 0) and the share of agent audit rows linked to a run.",
        _O.sm39_governance_integrity, gate=REPORT),
    _p4("smc14_rubber_stamping", "Rubber-stamp approvals", "kpi", "s",
        "SM-C14: share of approvals of unopened Proposals or within 5 seconds of opening, by agent.",
        _O.smc14_rubber_stamping, gate=REPORT, requires=_need("proposal_review_signals", "agent_proposals")),
    _p4("smc15_contact_fatigue", "Contact fatigue", "kpi", "s",
        "SM-C15: opt-outs and 'talk to a person' hand-offs per 1,000 agent messages.", _O.smc15_contact_fatigue,
        gate=REPORT, requires=_need("agent_tool_calls", "agent_runs")),
    _p4("smc16_impact_ratios", "Open bias audit", "kpi", "s",
        "SM-C16: impact ratios of the agent selection points are in the bias audit (no numbers here).",
        _O.smc16_impact_ratios, gate=REPORT),
    _p4("smc17_ai_cost_per_placement", "AI cost per placement", "kpi", "s",
        "SM-C17: agent model cost (USD) ÷ Placements started in range.", _O.smc17_ai_cost_per_placement,
        gate=(*REPORT, "placement:read"), requires=_need("placements")),
    _p4("smc18_wrong_answers", "Wrong answers", "kpi", "s",
        "SM-C18: Copilot unhelpful ratings and Onboarding answers marked wrong ÷ answers (target < 2%).",
        _O.smc18_wrong_answers, gate=REPORT, requires=_need("copilot_feedback")),
)  # fmt: skip

P4_DASHBOARDS: dict[str, DashboardDef] = {
    "agents": DashboardDef("agents", "AI agents", _AGENT_TILES),
    "agent_outcomes": DashboardDef("agent_outcomes", "Agent outcomes", _OUTCOME_TILES),
}
P4_GATES: dict[str, tuple[tuple[str, ...], ...]] = {"agents": (GOVERN,), "agent_outcomes": (REPORT,)}
P4_ORDER = ("agents", "agent_outcomes")
P4_KEYS = frozenset(P4_ORDER)


def p4_allowed(principal: Principal, key: str) -> bool:
    from app.services.reporting.filters import holds

    return any(all(holds(principal, p) for p in group) for group in P4_GATES[key])


def p4_keys(principal: Principal) -> list[str]:
    """Phase 4 dashboards the caller may open (after the P1-P3 ones)."""
    return [k for k in P4_ORDER if p4_allowed(principal, k)]


__all__ = ["P4_DASHBOARDS", "P4_GATES", "P4_KEYS", "P4_ORDER", "P4_TILES", "p4_allowed", "p4_keys"]
