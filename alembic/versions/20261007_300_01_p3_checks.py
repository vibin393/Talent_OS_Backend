# ruff: noqa: E501
"""Phase 3 foundation (Epic 36, stories 36.3 / 36.5): CHECK widenings on shared P1/P2 tables (spec 0.3).

* Consent purposes (``consents`` / ``consent_notices``) and sources (``consents``).
* Connector categories (``provider_connector_configs`` / ``connector_jobs`` / ``connector_inbound_events``).
* ``candidates.source``, ``applications.source``, ``interview_stages.kind`` (+``assessment``).
* ``channel_templates.purpose`` (+``marketing``), ``email_messages`` / ``channel_messages.origin`` (+``campaign``).
* ``tasks.entity_type`` and ``exports.kind`` (Phase 3 subjects / list kinds).

Downgrade narrows every CHECK back; it fails loudly (CHECK violation) if Phase 3 values exist -- no data is
deleted. The value lists are frozen here (migrations never import app code).

Revision ID: 20261007_300_01_p3_checks
Revises: 20261006_900_merge_phase2
Create Date: 2026-10-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261007_300_01_p3_checks"
down_revision: str | tuple[str, ...] | None = "20261006_900_merge_phase2"
branch_labels: str | Sequence[str] | None = ("p3_foundation",)
depends_on: str | Sequence[str] | None = None

P2_CONSENT_PURPOSES = (
    "data_processing", "ai_processing", "client_submission", "marketing",
    "bgv", "reference_check", "vendor_collection", "whatsapp", "engagement",
)  # fmt: skip
P3_CONSENT_PURPOSES = (
    *P2_CONSENT_PURPOSES, "assessment", "demographic_self_id", "referral", "campus", "immigration", "tracking",
)  # fmt: skip
P2_CONSENT_SOURCES = (
    "candidate_link", "candidate_portal", "application", "recruiter_attestation", "import",
    "vendor_attestation", "vendor_notice", "worker_portal", "placement", "whatsapp_optin", "recipient_link",
)  # fmt: skip
P3_CONSENT_SOURCES = (
    *P2_CONSENT_SOURCES,
    "campus_registration",
    "event_checkin",
    "referral_link",
    "self_id_form",
)
P2_CATEGORIES = ("esign", "bgv", "einvoice", "payroll", "accounting", "sms", "whatsapp")
P3_CATEGORIES = (*P2_CATEGORIES, "assessment", "dsc", "tds_statement", "fx")
P2_CANDIDATE_SOURCES = (
    "manual",
    "resume_upload",
    "bulk_import",
    "career_portal",
    "referral",
    "job_board",
    "other",
)
P3_CANDIDATE_SOURCES = (*P2_CANDIDATE_SOURCES, "campus", "event", "internal")
P2_APPLICATION_SOURCES = (
    "manual", "search", "match", "ai_match", "saved_list", "candidate_360", "career_portal", "import", "api", "vendor",
)  # fmt: skip
P3_APPLICATION_SOURCES = (*P2_APPLICATION_SOURCES, "internal", "referral", "campaign", "campus", "event")
P2_CHANNEL_PURPOSES = (
    "timesheet_reminder", "onboarding_task", "interview_reminder", "joining_touchpoint", "other_transactional",
)  # fmt: skip
P3_CHANNEL_PURPOSES = (*P2_CHANNEL_PURPOSES, "marketing")
P2_ORIGINS = ("user", "workflow", "system")
P3_ORIGINS = (*P2_ORIGINS, "campaign")
P2_TASK_ENTITY_TYPES = ("client", "contact", "demand", "candidate", "application", "lead", "opportunity")
P3_TASK_ENTITY_TYPES = (
    *P2_TASK_ENTITY_TYPES, "offer", "placement", "project", "deliverable", "immigration_case", "campus_drive",
    "workforce_plan", "referral", "talent_pool", "campaign",
)  # fmt: skip
P2_EXPORT_KINDS = (
    "candidates", "demands", "applications", "submissions", "interviews", "offers", "leads", "opportunities",
    "vendors", "vendor_submissions", "placements", "timesheets", "expense_claims", "invoices", "payments", "bgv_cases",
)  # fmt: skip
P3_EXPORT_KINDS = (
    *P2_EXPORT_KINDS, "talent_pools", "campaigns", "assessments", "campus_drives", "recruitment_events", "projects",
    "immigration_cases", "report_definitions", "referrals", "workforce_plans",
)  # fmt: skip
P2_STAGE_KINDS = ("technical", "hr", "client", "managerial", "other")
P3_STAGE_KINDS = (*P2_STAGE_KINDS, "assessment")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def _checks(phase3: bool) -> list[tuple[str, str, str]]:
    def pick(p2: tuple[str, ...], p3: tuple[str, ...]) -> tuple[str, ...]:
        return p3 if phase3 else p2

    purposes = pick(P2_CONSENT_PURPOSES, P3_CONSENT_PURPOSES)
    categories = pick(P2_CATEGORIES, P3_CATEGORIES)
    origins = pick(P2_ORIGINS, P3_ORIGINS)
    return [
        ("consents", "ck_consents_purpose", _in("purpose", purposes)),
        ("consent_notices", "ck_consent_notices_purpose", _in("purpose", purposes)),
        ("consents", "ck_consents_source", _in("source", pick(P2_CONSENT_SOURCES, P3_CONSENT_SOURCES))),
        ("provider_connector_configs", "ck_provider_connector_configs_category", _in("category", categories)),
        ("connector_jobs", "ck_connector_jobs_category", _in("category", categories)),
        ("connector_inbound_events", "ck_connector_inbound_events_category", _in("category", categories)),
        (
            "candidates",
            "ck_candidates_source",
            _in("source", pick(P2_CANDIDATE_SOURCES, P3_CANDIDATE_SOURCES)),
        ),
        (
            "applications",
            "ck_applications_source",
            _in("source", pick(P2_APPLICATION_SOURCES, P3_APPLICATION_SOURCES)),
        ),
        ("interview_stages", "ck_interview_stages_kind", _in("kind", pick(P2_STAGE_KINDS, P3_STAGE_KINDS))),
        (
            "channel_templates",
            "ck_channel_templates_purpose",
            _in("purpose", pick(P2_CHANNEL_PURPOSES, P3_CHANNEL_PURPOSES)),
        ),
        ("email_messages", "ck_email_messages_origin", _in("origin", origins)),
        ("channel_messages", "ck_channel_messages_origin", _in("origin", origins)),
        (
            "tasks",
            "ck_tasks_entity_type",
            f"entity_type IS NULL OR {_in('entity_type', pick(P2_TASK_ENTITY_TYPES, P3_TASK_ENTITY_TYPES))}",
        ),
        ("exports", "ck_exports_kind", _in("kind", pick(P2_EXPORT_KINDS, P3_EXPORT_KINDS))),
    ]


def upgrade() -> None:
    for table, name, condition in _checks(phase3=True):
        _replace_check(table, name, condition)


def downgrade() -> None:
    for table, name, condition in reversed(_checks(phase3=False)):
        _replace_check(table, name, condition)
