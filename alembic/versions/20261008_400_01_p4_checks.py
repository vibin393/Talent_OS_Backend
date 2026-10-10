# ruff: noqa: E501
"""Phase 4 foundation (Epic 53, story 53.1): CHECK widenings on shared P1-P3 tables (spec 0.3).

* ``audit_logs.actor_type`` and ``offer_status_history.actor_kind`` (+``agent``, AD-60).
* Consent purposes (``consents`` / ``consent_notices``: +``ai_agent_communication``, ``voice_screening``,
  ``call_recording``, ``interview_recording``, ``knowledge_assistant``) and sources (``consents``: +``conversation``,
  ``voice_consent_link``, ``interview_invite``).
* Connector categories (``provider_connector_configs`` / ``connector_jobs`` / ``connector_inbound_events``: +``voice``).
* ``email_messages`` / ``channel_messages.origin`` (+``agent``).
* ``aedt_inventory.feature`` (+ the five agent features, AD-71).
* ``exports.kind`` (+``agent_runs``, ``agent_proposals``).
* ``tasks.entity_type`` (+``agent_run``, ``agent_proposal``) and ``tasks.source`` (+``agent``; deviation recorded in the
  spec notes: system tasks created by agents need a CHECK-valid source).

Downgrade narrows every CHECK back; it fails loudly (CHECK violation) if Phase 4 values exist -- no data is
deleted. The value lists are frozen here (migrations never import app code).

Revision ID: 20261008_400_01_p4_checks
Revises: 20261007_900_merge_phase3
Create Date: 2026-10-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261008_400_01_p4_checks"
down_revision: str | tuple[str, ...] | None = "20261007_900_merge_phase3"
branch_labels: str | Sequence[str] | None = ("p4_foundation",)
depends_on: str | Sequence[str] | None = None

P3_ACTOR_TYPES = (
    "user", "candidate", "platform", "system", "ai", "workflow", "api_key", "client_user", "vendor_user", "secure_link",
)  # fmt: skip
P4_ACTOR_TYPES = (*P3_ACTOR_TYPES, "agent")
P3_OFFER_ACTOR_KINDS = ("user", "candidate", "system", "workflow", "ai", "api_key")
P4_OFFER_ACTOR_KINDS = (*P3_OFFER_ACTOR_KINDS, "agent")
P3_CONSENT_PURPOSES = (
    "data_processing", "ai_processing", "client_submission", "marketing",
    "bgv", "reference_check", "vendor_collection", "whatsapp", "engagement",
    "assessment", "demographic_self_id", "referral", "campus", "immigration", "tracking",
)  # fmt: skip
P4_CONSENT_PURPOSES = (
    *P3_CONSENT_PURPOSES, "ai_agent_communication", "voice_screening", "call_recording", "interview_recording",
    "knowledge_assistant",
)  # fmt: skip
P3_CONSENT_SOURCES = (
    "candidate_link", "candidate_portal", "application", "recruiter_attestation", "import",
    "vendor_attestation", "vendor_notice", "worker_portal", "placement", "whatsapp_optin", "recipient_link",
    "campus_registration", "event_checkin", "referral_link", "self_id_form",
)  # fmt: skip
P4_CONSENT_SOURCES = (*P3_CONSENT_SOURCES, "conversation", "voice_consent_link", "interview_invite")
P3_CATEGORIES = (
    "esign", "bgv", "einvoice", "payroll", "accounting", "sms", "whatsapp", "assessment", "dsc", "tds_statement", "fx",
)  # fmt: skip
P4_CATEGORIES = (*P3_CATEGORIES, "voice")
P3_ORIGINS = ("user", "workflow", "system", "campaign")
P4_ORIGINS = (*P3_ORIGINS, "agent")
P3_AEDT_FEATURES = ("ai_match", "ai_screening", "assessment_autoscore", "other")
P4_AEDT_FEATURES = (
    *P3_AEDT_FEATURES, "agent_sourcing", "agent_screening", "agent_voice_screening", "agent_interview_assist",
)  # fmt: skip
P3_EXPORT_KINDS = (
    "candidates", "demands", "applications", "submissions", "interviews", "offers", "leads", "opportunities",
    "vendors", "vendor_submissions", "placements", "timesheets", "expense_claims", "invoices", "payments", "bgv_cases",
    "talent_pools", "campaigns", "assessments", "campus_drives", "recruitment_events", "projects",
    "immigration_cases", "report_definitions", "referrals", "workforce_plans",
)  # fmt: skip
P4_EXPORT_KINDS = (*P3_EXPORT_KINDS, "agent_runs", "agent_proposals")
P3_TASK_ENTITY_TYPES = (
    "client", "contact", "demand", "candidate", "application", "lead", "opportunity", "offer", "placement", "project",
    "deliverable", "immigration_case", "campus_drive", "workforce_plan", "referral", "talent_pool", "campaign",
)  # fmt: skip
P4_TASK_ENTITY_TYPES = (*P3_TASK_ENTITY_TYPES, "agent_run", "agent_proposal")
P3_TASK_SOURCES = ("manual", "workflow", "onboarding", "offboarding", "collections", "client_portal", "bgv")
P4_TASK_SOURCES = (*P3_TASK_SOURCES, "agent")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def _checks(phase4: bool) -> list[tuple[str, str, str]]:
    def pick(p3: tuple[str, ...], p4: tuple[str, ...]) -> tuple[str, ...]:
        return p4 if phase4 else p3

    purposes = pick(P3_CONSENT_PURPOSES, P4_CONSENT_PURPOSES)
    categories = pick(P3_CATEGORIES, P4_CATEGORIES)
    origins = pick(P3_ORIGINS, P4_ORIGINS)
    return [
        ("audit_logs", "ck_audit_logs_actor_type", _in("actor_type", pick(P3_ACTOR_TYPES, P4_ACTOR_TYPES))),
        (
            "offer_status_history",
            "ck_offer_status_history_actor_kind",
            _in("actor_kind", pick(P3_OFFER_ACTOR_KINDS, P4_OFFER_ACTOR_KINDS)),
        ),
        ("consents", "ck_consents_purpose", _in("purpose", purposes)),
        ("consent_notices", "ck_consent_notices_purpose", _in("purpose", purposes)),
        ("consents", "ck_consents_source", _in("source", pick(P3_CONSENT_SOURCES, P4_CONSENT_SOURCES))),
        ("provider_connector_configs", "ck_provider_connector_configs_category", _in("category", categories)),
        ("connector_jobs", "ck_connector_jobs_category", _in("category", categories)),
        ("connector_inbound_events", "ck_connector_inbound_events_category", _in("category", categories)),
        ("email_messages", "ck_email_messages_origin", _in("origin", origins)),
        ("channel_messages", "ck_channel_messages_origin", _in("origin", origins)),
        (
            "aedt_inventory",
            "ck_aedt_inventory_feature",
            _in("feature", pick(P3_AEDT_FEATURES, P4_AEDT_FEATURES)),
        ),
        ("exports", "ck_exports_kind", _in("kind", pick(P3_EXPORT_KINDS, P4_EXPORT_KINDS))),
        (
            "tasks",
            "ck_tasks_entity_type",
            f"entity_type IS NULL OR {_in('entity_type', pick(P3_TASK_ENTITY_TYPES, P4_TASK_ENTITY_TYPES))}",
        ),
        ("tasks", "ck_tasks_source", _in("source", pick(P3_TASK_SOURCES, P4_TASK_SOURCES))),
    ]


def upgrade() -> None:
    for table, name, condition in _checks(phase4=True):
        _replace_check(table, name, condition)


def downgrade() -> None:
    for table, name, condition in reversed(_checks(phase4=False)):
        _replace_check(table, name, condition)
