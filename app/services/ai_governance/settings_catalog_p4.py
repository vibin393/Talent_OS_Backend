# ruff: file-ignore[line-too-long]
"""Phase 4 settings hub catalogue (Epic 65 story 65.8, FR-329/FR-330).

Owners register their own hub entries from ``hooks.py``; this module adds only the entries no owner registered (an
owner's own registration always wins). ``available`` stays route-existence based. ``P4_REQUIRED_SETTINGS`` lists every
Phase 4 configuration the hub must list, with its owner epic.
"""

from app.services.settings_service import register_settings_entry, registered_settings_entries

AI, RECRUITING = "ai", "recruiting"

# key -> owner epic (owners' keys + this module's).
P4_REQUIRED_SETTINGS: dict[str, str] = {
    "agentic": "Epic 65",
    "agents": "Epic 65",
    "ai_kill_switch": "Epic 65",
    "ai_governance": "Epic 65",
    "ai_evals": "Epic 65",
    "copilot": "Epic 54",
    "demand_intake": "Epic 55",
    "demand_agent": "Epic 55",
    "sourcing_agent": "Epic 56",
    "screening_agent": "Epic 57",
    "screening_questionnaires": "Epic 57",
    "voice_screening": "Epic 58",
    "recording_policy": "Epic 58",
    "scheduling_agent": "Epic 59",
    "interview_assistant": "Epic 60",
    "client_submission_rules": "Epic 61",
    "knowledge_sources": "Epic 62",
    "collection_rules": "Epic 63",
}

P4_ENTRIES: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    (AI, "agentic", "AI agents", "Turn agents on for this tenant, accept the enablement notice and see each agent's readiness.",
     "/api/v1/ai/agentic", "ai:agent_configure", "Epic 65"),
    (AI, "agents", "Agent settings", "Autonomy per action, triggers, Proposal routing and expiry, budgets, limits, channels and working hours.",
     "/api/v1/agents", "ai:agent_configure", "Epic 65"),
    (AI, "ai_kill_switch", "Agent kill switch", "Stop all agent and Copilot activity at once.",
     "/api/v1/ai/kill-switch", "ai:kill_switch", "Epic 65"),
    (AI, "ai_governance", "AI governance console", "Agent runs, action log, policy violations, costs and Copilot usage.",
     "/api/v1/ai-governance/summary", "ai:govern", "Epic 65"),
    (AI, "ai_evals", "Evaluation results", "Latest evaluation and red-team results for each agent.",
     "/api/v1/ai-governance/evals", "ai:govern", "Epic 65"),
    (RECRUITING, "screening_questionnaires", "Screening questionnaires", "Questions the Screening Agent asks, by Demand or stage.",
     "/api/v1/pipeline/questionnaires", "screening:conduct", "Epic 57"),
)  # fmt: skip


def register() -> None:
    have = {e.key for e in registered_settings_entries()}
    for category, key, label, description, endpoint, permission, owner in P4_ENTRIES:
        if key not in have:
            register_settings_entry(category, key, label, description, endpoint, permission, owner)


__all__ = ["P4_ENTRIES", "P4_REQUIRED_SETTINGS", "register"]
