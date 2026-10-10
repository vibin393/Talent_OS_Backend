# ruff: noqa: E501
"""Phase 5 foundation (Epic 66, story 66.1): CHECK widenings on shared P1-P4 tables (spec 0.3).

* ``audit_logs.actor_type`` and ``offer_status_history.actor_kind`` (+``app``, AD-75; the only CHECKs listing ``api_key``).
* Connector categories (``provider_connector_configs`` / ``connector_jobs`` / ``connector_inbound_events``: +``job_board``,
  ``hrms``, ``stt``) and ``connector_inbound_events.status`` (+``held``: Ecosystem Kill Switch, FR-340).
* ``agent_runs.origin`` / ``trigger_kind`` and ``agent_proposals.origin`` (+``external``, ``portal``; AD-84/AD-89).
* Consent purposes (``consents`` / ``consent_notices``: +``job_board_application``, ``job_network_application``,
  ``hrms_transfer``, ``portal_copilot``) and sources (``consents``: +``job_board``, ``job_network``, ``public_api``).
* ``candidates.source`` (+``job_network``, ``public_api``; ``job_board`` exists) and ``applications.source``
  (+``job_board``, ``job_network``, ``public_api``).
* ``tasks.entity_type`` (+ every Phase 5 fix-it subject) and ``exports.kind`` (+``api_usage``, ``webhook_deliveries``,
  ``connector_sync``).

Downgrade narrows every CHECK back; it fails loudly (CHECK violation) if Phase 5 values exist -- no data is deleted.
The value lists are frozen here (migrations never import app code).

Revision ID: 20261009_500_01_p5_checks
Revises: 20261009_900_merge_phase4
Create Date: 2026-10-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261009_500_01_p5_checks"
down_revision: str | tuple[str, ...] | None = "20261009_900_merge_phase4"
branch_labels: str | Sequence[str] | None = ("p5_foundation",)
depends_on: str | Sequence[str] | None = None

P4_ACTOR_TYPES = (
    "user", "candidate", "platform", "system", "ai", "workflow", "api_key", "client_user", "vendor_user", "secure_link",
    "agent",
)  # fmt: skip
P5_ACTOR_TYPES = (*P4_ACTOR_TYPES, "app")
P4_OFFER_ACTOR_KINDS = ("user", "candidate", "system", "workflow", "ai", "api_key", "agent")
P5_OFFER_ACTOR_KINDS = (*P4_OFFER_ACTOR_KINDS, "app")
P4_CATEGORIES = (
    "esign", "bgv", "einvoice", "payroll", "accounting", "sms", "whatsapp", "assessment", "dsc", "tds_statement", "fx",
    "voice",
)  # fmt: skip
P5_CATEGORIES = (*P4_CATEGORIES, "job_board", "hrms", "stt")
P4_INBOUND_STATUSES = ("received", "processed", "failed")
P5_INBOUND_STATUSES = (*P4_INBOUND_STATUSES, "held")
P4_RUN_ORIGINS = ("agent", "copilot", "conversation")
P5_RUN_ORIGINS = (*P4_RUN_ORIGINS, "external", "portal")
P4_TRIGGER_KINDS = ("event", "schedule", "workflow", "manual", "copilot", "inbound")
P5_TRIGGER_KINDS = (*P4_TRIGGER_KINDS, "external", "portal")
P4_PROPOSAL_ORIGINS = ("agent", "copilot")
P5_PROPOSAL_ORIGINS = (*P4_PROPOSAL_ORIGINS, "external", "portal")
P4_CONSENT_PURPOSES = (
    "data_processing", "ai_processing", "client_submission", "marketing",
    "bgv", "reference_check", "vendor_collection", "whatsapp", "engagement",
    "assessment", "demographic_self_id", "referral", "campus", "immigration", "tracking",
    "ai_agent_communication", "voice_screening", "call_recording", "interview_recording", "knowledge_assistant",
)  # fmt: skip
P5_CONSENT_PURPOSES = (
    *P4_CONSENT_PURPOSES, "job_board_application", "job_network_application", "hrms_transfer", "portal_copilot",
)  # fmt: skip
P4_CONSENT_SOURCES = (
    "candidate_link", "candidate_portal", "application", "recruiter_attestation", "import",
    "vendor_attestation", "vendor_notice", "worker_portal", "placement", "whatsapp_optin", "recipient_link",
    "campus_registration", "event_checkin", "referral_link", "self_id_form",
    "conversation", "voice_consent_link", "interview_invite",
)  # fmt: skip
P5_CONSENT_SOURCES = (*P4_CONSENT_SOURCES, "job_board", "job_network", "public_api")
P4_CANDIDATE_SOURCES = (
    "manual", "resume_upload", "bulk_import", "career_portal", "referral", "job_board", "other", "campus", "event",
    "internal",
)  # fmt: skip
P5_CANDIDATE_SOURCES = (*P4_CANDIDATE_SOURCES, "job_network", "public_api")
P4_APPLICATION_SOURCES = (
    "manual", "search", "match", "ai_match", "saved_list", "candidate_360", "career_portal", "import", "api", "vendor",
    "internal", "referral", "campaign", "campus", "event",
)  # fmt: skip
P5_APPLICATION_SOURCES = (*P4_APPLICATION_SOURCES, "job_board", "job_network", "public_api")
P4_TASK_ENTITY_TYPES = (
    "client", "contact", "demand", "candidate", "application", "lead", "opportunity", "offer", "placement", "project",
    "deliverable", "immigration_case", "campus_drive", "workforce_plan", "referral", "talent_pool", "campaign",
    "agent_run", "agent_proposal",
)  # fmt: skip
P5_TASK_ENTITY_TYPES = (
    *P4_TASK_ENTITY_TYPES, "installation", "webhook_subscription", "board_posting", "hrms_sync", "ledger_sync",
    "payment_suggestion", "calendar_change", "network_listing", "mcp_session",
)  # fmt: skip
P4_EXPORT_KINDS = (
    "candidates", "demands", "applications", "submissions", "interviews", "offers", "leads", "opportunities",
    "vendors", "vendor_submissions", "placements", "timesheets", "expense_claims", "invoices", "payments", "bgv_cases",
    "talent_pools", "campaigns", "assessments", "campus_drives", "recruitment_events", "projects",
    "immigration_cases", "report_definitions", "referrals", "workforce_plans", "agent_runs", "agent_proposals",
)  # fmt: skip
P5_EXPORT_KINDS = (*P4_EXPORT_KINDS, "api_usage", "webhook_deliveries", "connector_sync")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def _checks(phase5: bool) -> list[tuple[str, str, str]]:
    def pick(p4: tuple[str, ...], p5: tuple[str, ...]) -> tuple[str, ...]:
        return p5 if phase5 else p4

    purposes = pick(P4_CONSENT_PURPOSES, P5_CONSENT_PURPOSES)
    categories = pick(P4_CATEGORIES, P5_CATEGORIES)
    return [
        ("audit_logs", "ck_audit_logs_actor_type", _in("actor_type", pick(P4_ACTOR_TYPES, P5_ACTOR_TYPES))),
        (
            "offer_status_history",
            "ck_offer_status_history_actor_kind",
            _in("actor_kind", pick(P4_OFFER_ACTOR_KINDS, P5_OFFER_ACTOR_KINDS)),
        ),
        ("provider_connector_configs", "ck_provider_connector_configs_category", _in("category", categories)),
        ("connector_jobs", "ck_connector_jobs_category", _in("category", categories)),
        ("connector_inbound_events", "ck_connector_inbound_events_category", _in("category", categories)),
        (
            "connector_inbound_events",
            "ck_connector_inbound_events_status",
            _in("status", pick(P4_INBOUND_STATUSES, P5_INBOUND_STATUSES)),
        ),
        ("agent_runs", "ck_agent_runs_origin", _in("origin", pick(P4_RUN_ORIGINS, P5_RUN_ORIGINS))),
        (
            "agent_runs",
            "ck_agent_runs_trigger_kind",
            _in("trigger_kind", pick(P4_TRIGGER_KINDS, P5_TRIGGER_KINDS)),
        ),
        (
            "agent_proposals",
            "ck_agent_proposals_origin",
            _in("origin", pick(P4_PROPOSAL_ORIGINS, P5_PROPOSAL_ORIGINS)),
        ),
        ("consents", "ck_consents_purpose", _in("purpose", purposes)),
        ("consent_notices", "ck_consent_notices_purpose", _in("purpose", purposes)),
        ("consents", "ck_consents_source", _in("source", pick(P4_CONSENT_SOURCES, P5_CONSENT_SOURCES))),
        (
            "candidates",
            "ck_candidates_source",
            _in("source", pick(P4_CANDIDATE_SOURCES, P5_CANDIDATE_SOURCES)),
        ),
        (
            "applications",
            "ck_applications_source",
            _in("source", pick(P4_APPLICATION_SOURCES, P5_APPLICATION_SOURCES)),
        ),
        (
            "tasks",
            "ck_tasks_entity_type",
            f"entity_type IS NULL OR {_in('entity_type', pick(P4_TASK_ENTITY_TYPES, P5_TASK_ENTITY_TYPES))}",
        ),
        ("exports", "ck_exports_kind", _in("kind", pick(P4_EXPORT_KINDS, P5_EXPORT_KINDS))),
    ]


def upgrade() -> None:
    for table, name, condition in _checks(phase5=True):
        _replace_check(table, name, condition)


def downgrade() -> None:
    for table, name, condition in reversed(_checks(phase5=False)):
        _replace_check(table, name, condition)
