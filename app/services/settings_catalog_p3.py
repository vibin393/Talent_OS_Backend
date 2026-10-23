# ruff: file-ignore[line-too-long]
"""Phase 3 settings hub catalogue (Epic 52 A5, FR-261).

Most Phase 3 owners register their own hub entry from ``hooks.py`` (``REQUIRED_P3_SETTINGS`` lists them with the
owner epic). This module registers the Phase 3 configuration pages that no owner registered, pointing at the owners'
real endpoints (endpoint and permission read from the owner routers at B4). An owner's own registration always wins:
an entry is added only when its key is absent. ``available`` stays route-existence based.
"""

from app.services.settings_service import register_settings_entry, registered_settings_entries

RECRUITING, WORKFORCE, FINANCE, COMPLIANCE, REPORTING, SERVICES, SECURITY = (
    "recruiting", "workforce", "finance", "compliance", "reporting", "services", "security_privacy",
)  # fmt: skip

# key -> owner epic: every Phase 3 configuration the hub must list (owners' keys + this module's).
REQUIRED_P3_SETTINGS: dict[str, str] = {
    "skills_graph": "Epic 37",
    "talent_pools": "Epic 38",
    "pool_templates": "Epic 38",
    "marketing": "Epic 39",
    "assessments": "Epic 40",
    "assessment_provider": "Epic 40",
    "mobility": "Epic 41",
    "referrals": "Epic 42",
    "campus": "Epic 43",
    "workforce": "Epic 44",
    "project_budgets": "Epic 45",
    "immigration": "Epic 46",
    "report_builder": "Epic 47",
    "report_schedules": "Epic 47",
    "analytics": "Epic 48",
    "prediction_models": "Epic 48",
    "fairness": "Epic 49",
    "aedt_inventory": "Epic 49",
    "client_sso": "Epic 50",
    "dsc_signing": "Epic 51",
    "tds_deductor_tans": "Epic 51",
    "fx_feed": "Epic 51",
}

# (category, key, label, description, endpoint, permission, owner_epic) -- entries no owner registered.
P3_ENTRIES: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    (RECRUITING, "pool_templates", "Talent pool templates", "Ready-made pools (silver medalists, alumni, campus batches).",
     "/api/v1/talent-pools/templates", "pool:manage", "Epic 38"),
    (SERVICES, "assessment_provider", "Assessment provider", "Connect an external assessment provider and pick its tests.",
     "/api/v1/assessments/provider-tests", "assessment:configure", "Epic 40"),
    (WORKFORCE, "workforce", "Workforce planning", "Plan defaults, standard hours and days, client forecast rules.",
     "/api/v1/workforce/settings", "workforce_plan:approve", "Epic 44"),
    (WORKFORCE, "project_budgets", "Project budgets", "Hours, cost and revenue budgets and alert thresholds per project.",
     "/api/v1/projects/{project_id}/budgets", "project:write", "Epic 45"),
    (REPORTING, "report_schedules", "Report schedules", "Scheduled custom reports and their internal recipients.",
     "/api/v1/report-schedules", "report:schedule", "Epic 47"),
    (RECRUITING, "prediction_models", "Prediction models", "Model status, training data size and metrics per kind.",
     "/api/v1/analytics/models", "model:manage", "Epic 48"),
    (COMPLIANCE, "aedt_inventory", "AEDT inventory", "Automated employment decision tools in use (NYC LL144).",
     "/api/v1/fairness/aedt", "aedt:manage", "Epic 49"),
    (FINANCE, "tds_deductor_tans", "TDS deductor TANs", "Map 26AS deductor TANs to Clients for TDS reconciliation.",
     "/api/v1/receivables/tds-recon/deductor-tans", "tds_recon:write", "Epic 51"),
)  # fmt: skip


def register() -> None:
    present = {e.key for e in registered_settings_entries()}
    for category, key, label, description, endpoint, permission, owner in P3_ENTRIES:
        if key not in present:
            register_settings_entry(category, key, label, description, endpoint, permission, owner)


register()
