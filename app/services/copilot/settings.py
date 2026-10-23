# ruff: file-ignore[line-too-long]
"""Copilot settings rules and the availability decision (Epic 54 A1 / A5 ``status``). Pure: no I/O.

Turning the Copilot on or off is the foundation's ``PUT /agents/copilot/enabled`` (AI feature ``agent_copilot``);
draft expiry is ``agent_configs.proposal_expiry_hours`` of ``copilot`` (default 24 h, the foundation expiry job)."""

from collections.abc import Mapping
from typing import Literal

DRAFT_EXPIRY_DEFAULT_HOURS = 24
DRAFT_EXPIRY_MAX_HOURS = 72
SETTINGS_FIELDS = (
    "retention_days",
    "questions_per_hour",
    "max_open_conversations",
    "allow_feedback_comments",
)

Reason = Literal["no_permission", "not_enabled", "kill_switch", "global_kill", "budget_exhausted"]


def unavailable_reason(
    *,
    has_permission: bool,
    features: Mapping[str, bool],
    agent_key: str = "copilot",
    tenant_kill: bool,
    global_kill: bool,
    budget_exhausted: bool,
) -> Reason | None:
    """Why the Copilot cannot answer now (None = available). Order: permission, global kill, tenant kill, feature
    toggles (``agentic`` master + ``agent_<key>``), monthly AI allowance."""
    if not has_permission:
        return "no_permission"
    if global_kill:
        return "global_kill"
    if tenant_kill:
        return "kill_switch"
    if not features.get("agentic", False) or not features.get(f"agent_{agent_key}", False):
        return "not_enabled"
    if budget_exhausted:
        return "budget_exhausted"
    return None


__all__ = [
    "DRAFT_EXPIRY_DEFAULT_HOURS",
    "DRAFT_EXPIRY_MAX_HOURS",
    "SETTINGS_FIELDS",
    "Reason",
    "unavailable_reason",
]
