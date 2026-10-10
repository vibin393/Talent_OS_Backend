# ruff: noqa: E501
"""Phase 5 settings hub catalogue (Epic 79 story 79.4, FR-412 / spec A6).

Owners register their own hub entries from ``hooks.py``; this module adds only the entries no owner registered (an
owner's own registration always wins). ``REQUIRED_P5_SETTINGS`` lists every Phase 5 configuration the hub must list,
with its owner epic (``tests/governance/test_settings_catalog.py``).
"""

from app.services.settings_service import register_settings_entry, registered_settings_entries

INTEGRATIONS, AI, RECRUITING, MARKETING = "integrations", "ai", "recruiting", "marketing"

# key -> owner epic (owners' keys + this module's).
REQUIRED_P5_SETTINGS: dict[str, str] = {
    "ecosystem": "Epic 66 / 79",
    "marketplace": "Epic 69",
    "app_policy": "Epic 66 / 69",
    "webhooks": "Epic 68",
    "private_apps": "Epic 67",
    "job_boards": "Epic 70",
    "hrms_sync": "Epic 71",
    "ledger_sync": "Epic 71",
    "calendar_sync": "Epic 72",
    "mcp": "Epic 73",
    "job_network": "Epic 75",
    "telephony": "Epic 76",
    "portal_copilot": "Epic 77",
    "campaign_copy": "Epic 78",
    "access_reviews": "Epic 79",
    "usage": "Epic 66",
}

# category, key, label, description, endpoint, permission, owner
P5_ENTRIES: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    (INTEGRATIONS, "ecosystem", "Integrations and external access", "Everything that connects to Talent OS from outside: Installations, authorisations, webhooks, MCP and the Ecosystem Kill Switch.",
     "/api/v1/ecosystem", "integration:view", "Epic 66 / 79"),
    (INTEGRATIONS, "marketplace", "Marketplace", "Browse and install Apps and connectors.",
     "/api/v1/marketplace/listings", "integration:view", "Epic 69"),
    (INTEGRATIONS, "app_policy", "App policy", "Which Apps and scopes are allowed for this tenant.",
     "/api/v1/ecosystem/app-policy", "integration:manage", "Epic 66 / 69"),
    (INTEGRATIONS, "webhooks", "Webhooks", "Outbound event subscriptions and their delivery health.",
     "/api/v1/webhooks/subscriptions", "webhook:manage", "Epic 68"),
    (INTEGRATIONS, "private_apps", "Private apps", "Apps your developers build for this tenant only.",
     "/api/v1/apps", "app:develop", "Epic 67"),
    (INTEGRATIONS, "job_boards", "Job boards", "Connect external job boards and publish postings.",
     "/api/v1/job-boards", "posting:publish_external", "Epic 70"),
    (INTEGRATIONS, "hrms_sync", "HRMS sync", "Map HRMS fields and push Workers to the HR system.",
     "/api/v1/hrms/settings", "hrms:sync", "Epic 71"),
    (INTEGRATIONS, "ledger_sync", "Ledger sync", "Map ledgers and push finance documents to the accounting system.",
     "/api/v1/ledger/settings", "ledger:sync", "Epic 71"),
    (INTEGRATIONS, "calendar_sync", "Calendar sync", "Connect recruiter and organisation calendars.",
     "/api/v1/calendar-sync/settings", "integration:manage", "Epic 72"),
    (AI, "mcp", "MCP access for AI assistants", "Let external AI assistants connect through the governed MCP server.",
     "/api/v1/mcp/settings", "ai:agent_configure", "Epic 73"),
    (RECRUITING, "job_network", "Job network", "Join the Talent OS Jobs network and set listing defaults.",
     "/api/v1/job-network/settings", "network:manage", "Epic 75"),
    (INTEGRATIONS, "telephony", "Calling and transcription", "Calling provider, caller IDs, recording policy and transcription.",
     "/api/v1/settings/telephony", "integration:manage", "Epic 76"),
    (AI, "portal_copilot", "Portal Copilot", "The AI Copilot in the client, vendor and candidate portals.",
     "/api/v1/copilot/portal-admin/settings", "ai:agent_configure", "Epic 77"),
    (MARKETING, "campaign_copy", "AI campaign copy", "Draft campaign copy with AI and route it for approval.",
     "/api/v1/agents/campaign_copy", "ai:agent_configure", "Epic 78"),
    (INTEGRATIONS, "access_reviews", "Access reviews", "Periodic reviews of Installations, authorisations and API keys.",
     "/api/v1/ecosystem-console/access-reviews/settings", "integration:manage", "Epic 79"),
    (INTEGRATIONS, "usage", "Usage", "API, webhook, connector and telephony usage for this tenant.",
     "/api/v1/ecosystem/usage", "usage:read", "Epic 66"),
)


def register() -> None:
    have = {e.key for e in registered_settings_entries()}
    for category, key, label, description, endpoint, permission, owner in P5_ENTRIES:
        if key not in have:
            register_settings_entry(category, key, label, description, endpoint, permission, owner)


__all__ = ["P5_ENTRIES", "REQUIRED_P5_SETTINGS", "register"]
