"""Epic 6 pipeline core (tenant-owned, RLS): settings, stages (+ seed), reason codes (+ seed),
applications, stage history, screening questionnaires (+ seed), screenings.

Branch ``pipeline`` (spec A11). Consents, notices and secure links already exist (Epic 12,
20261001_121), so the spec's ``20261001_60`` has nothing left to create and is not needed. Defaults
for existing tenants are seeded before RLS is enabled; new tenants get them from
``ensure_default_stages`` (provisioning task, lazily on use).

Revision ID: 20261001_61
Revises: 20261001_53
Create Date: 2026-10-01

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_61"
down_revision: str | tuple[str, ...] | None = "20261001_53"
branch_labels: str | Sequence[str] | None = ("pipeline",)
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = (
    "pipeline_settings",
    "pipeline_stages",
    "pipeline_reason_codes",
    "applications",
    "application_stage_history",
    "screening_questionnaires",
    "screenings",
)

SEED_STAGES = """
    INSERT INTO pipeline_stages (id, tenant_id, key, label, kind, position)
    SELECT gen_random_uuid(), t.id, v.key, v.label, v.kind, v.position
    FROM tenants t CROSS JOIN (VALUES
        ('applied', 'Applied', 'anchored', 0),
        ('screening', 'Screening', 'custom', 10),
        ('shortlisted', 'Shortlisted', 'anchored', 100),
        ('submitted', 'Submitted', 'anchored', 110),
        ('interviewing', 'Interviewing', 'anchored', 120),
        ('selected', 'Selected', 'anchored', 130),
        ('offered', 'Offered', 'anchored', 140),
        ('hired', 'Hired', 'anchored', 150),
        ('rejected', 'Rejected', 'anchored', 160),
        ('withdrawn', 'Withdrawn', 'anchored', 170)
    ) AS v(key, label, kind, position)
"""
SEED_REASONS = """
    INSERT INTO pipeline_reason_codes (id, tenant_id, kind, code, label)
    SELECT gen_random_uuid(), t.id, v.kind, v.code, v.label
    FROM tenants t CROSS JOIN (VALUES
        ('reject', 'not_qualified', 'Not qualified'),
        ('reject', 'compensation_mismatch', 'Compensation mismatch'),
        ('reject', 'notice_period', 'Notice period'),
        ('reject', 'location', 'Location'),
        ('reject', 'no_response', 'No response'),
        ('reject', 'client_rejected', 'Client rejected'),
        ('reject', 'interview_rejected', 'Interview rejected'),
        ('reject', 'duplicate', 'Duplicate'),
        ('reject', 'other', 'Other'),
        ('withdraw', 'not_interested', 'Not interested'),
        ('withdraw', 'accepted_other_offer', 'Accepted another offer'),
        ('withdraw', 'compensation', 'Compensation'),
        ('withdraw', 'personal', 'Personal reasons'),
        ('withdraw', 'other', 'Other')
    ) AS v(kind, code, label)
"""
DEFAULT_QUESTIONS: list[dict[str, object]] = [
    {
        "key": "interest",
        "label": "Is the candidate interested in this role?",
        "type": "boolean",
        "required": True,
    },
    {
        "key": "availability",
        "label": "Earliest date the candidate can start",
        "type": "date",
        "required": False,
    },
    {
        "key": "expected_compensation",
        "label": "Expected compensation",
        "type": "money",
        "required": False,
        "maps_to": "expected_ctc",
    },
    {
        "key": "notice_period",
        "label": "Notice period (days)",
        "type": "number",
        "required": False,
        "maps_to": "notice_period_days",
    },
    {
        "key": "work_authorization",
        "label": "Work authorization",
        "type": "text",
        "required": False,
        "maps_to": "work_authorization",
    },
    {
        "key": "relocation",
        "label": "Willing to relocate?",
        "type": "boolean",
        "required": False,
        "maps_to": "willing_to_relocate",
    },
    {"key": "communication", "label": "Communication (1-5)", "type": "rating", "required": False},
]
SEED_QUESTIONNAIRE = """
    INSERT INTO screening_questionnaires (id, tenant_id, name, questions, is_default)
    SELECT gen_random_uuid(), t.id, 'Default screening', CAST(:questions AS jsonb), true
    FROM tenants t
"""


def upgrade() -> None:
    op.create_table(
        "pipeline_reason_codes",
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind IN ('reject', 'withdraw', 'reopen')", name=op.f("ck_pipeline_reason_codes_kind")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_pipeline_reason_codes_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_reason_codes")),
        sa.UniqueConstraint(
            "tenant_id", "kind", "code", name=op.f("uq_pipeline_reason_codes_tenant_id_kind_code")
        ),
    )
    op.create_index(
        op.f("ix_pipeline_reason_codes_tenant_id"), "pipeline_reason_codes", ["tenant_id"], unique=False
    )
    op.create_table(
        "pipeline_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "submission_consent_mode",
            sa.String(length=30),
            server_default=sa.text("'attestation_allowed'"),
            nullable=False,
        ),
        sa.Column("prior_submission_window_days", sa.Integer(), server_default=sa.text("90"), nullable=False),
        sa.Column(
            "min_margin_pct", sa.Numeric(precision=5, scale=2), server_default=sa.text("15"), nullable=False
        ),
        sa.Column(
            "burden_pct",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(
                '\'{"contract": 18, "contract_to_hire": 18, "permanent": 0, "freelance": 0}\'::jsonb'
            ),
            nullable=False,
        ),
        sa.Column("rejection_email_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("rejection_email_subject", sa.String(length=300), nullable=True),
        sa.Column("rejection_email_body", sa.Text(), nullable=True),
        sa.Column(
            "auto_interview_on_interest", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "show_bill_rate_in_submission", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "submission_consent_mode IN ('candidate_link', 'attestation_allowed')",
            name=op.f("ck_pipeline_settings_consent_mode"),
        ),
        sa.CheckConstraint(
            "min_margin_pct BETWEEN -100 AND 100", name=op.f("ck_pipeline_settings_min_margin")
        ),
        sa.CheckConstraint(
            "prior_submission_window_days BETWEEN 0 AND 3650", name=op.f("ck_pipeline_settings_prior_window")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_pipeline_settings_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_pipeline_settings")),
    )
    op.create_table(
        "pipeline_stages",
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('anchored', 'custom')", name=op.f("ck_pipeline_stages_kind")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_pipeline_stages_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_stages")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_pipeline_stages_tenant_id_id")),
        sa.UniqueConstraint("tenant_id", "key", name=op.f("uq_pipeline_stages_tenant_id_key")),
    )
    op.create_index(op.f("ix_pipeline_stages_tenant_id"), "pipeline_stages", ["tenant_id"], unique=False)
    op.create_table(
        "screening_questionnaires",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("questions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_screening_questionnaires_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_screening_questionnaires")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_screening_questionnaires_tenant_id_id")),
    )
    op.create_index(
        op.f("ix_screening_questionnaires_tenant_id"), "screening_questionnaires", ["tenant_id"], unique=False
    )
    op.create_index(
        "uq_screening_questionnaires_default",
        "screening_questionnaires",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_default AND archived_at IS NULL"),
    )
    op.create_table(
        "applications",
        sa.Column("human_id", sa.String(length=20), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("stage_id", sa.Uuid(), nullable=False),
        sa.Column("stage_entered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("source_ref", sa.String(length=200), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("screening_passed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_client_feedback", sa.String(length=20), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "last_client_feedback IS NULL OR last_client_feedback IN ('interested', 'rejected', 'on_hold')",
            name=op.f("ck_applications_last_feedback"),
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'search', 'match', 'ai_match', 'saved_list', 'candidate_360', "
            "'career_portal', 'import', 'api')",
            name=op.f("ck_applications_source"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_applications_candidate_id_candidates",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name="fk_applications_demand_id_demands",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_id"],
            ["users.tenant_id", "users.id"],
            name="fk_applications_owner_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "stage_id"],
            ["pipeline_stages.tenant_id", "pipeline_stages.id"],
            name="fk_applications_stage_id_pipeline_stages",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_applications_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_applications")),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "demand_id",
            name=op.f("uq_applications_tenant_id_candidate_id_demand_id"),
        ),
        sa.UniqueConstraint("tenant_id", "human_id", name=op.f("uq_applications_tenant_id_human_id")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_applications_tenant_id_id")),
    )
    op.create_index("ix_applications_candidate_id", "applications", ["candidate_id"], unique=False)
    op.create_index(
        "ix_applications_demand_id_stage_id", "applications", ["demand_id", "stage_id"], unique=False
    )
    op.create_index(op.f("ix_applications_tenant_id"), "applications", ["tenant_id"], unique=False)
    op.create_table(
        "application_stage_history",
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("from_stage_id", sa.Uuid(), nullable=True),
        sa.Column("to_stage_id", sa.Uuid(), nullable=False),
        sa.Column("actor_kind", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("reason_code", sa.String(length=50), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            name="fk_application_stage_history_application_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_application_stage_history_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_stage_history")),
    )
    op.create_index(
        "ix_application_stage_history_application_id_at",
        "application_stage_history",
        ["application_id", "at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_application_stage_history_tenant_id"),
        "application_stage_history",
        ["tenant_id"],
        unique=False,
    )
    op.create_table(
        "screenings",
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("questionnaire_id", sa.Uuid(), nullable=True),
        sa.Column(
            "questions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "answers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("outcome", sa.String(length=10), nullable=True),
        sa.Column("reason_code", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("ai_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ai_generated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("ai_action_id", sa.Uuid(), nullable=True),
        sa.Column("questions_ai_action_id", sa.Uuid(), nullable=True),
        sa.Column("summary_status", sa.String(length=20), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("secure_link_id", sa.Uuid(), nullable=True),
        sa.Column("invite_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invite_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("completed_by", sa.Uuid(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('recruiter', 'ai')", name=op.f("ck_screenings_kind")),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('proceed', 'hold', 'reject')", name=op.f("ck_screenings_outcome")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'sent', 'consent_declined', 'answered', 'reviewed', 'completed', 'expired')",
            name=op.f("ck_screenings_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            name="fk_screenings_application_id_applications",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_screenings_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_screenings")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_screenings_tenant_id_id")),
    )
    op.create_index("ix_screenings_application_id", "screenings", ["application_id"], unique=False)
    op.create_index("ix_screenings_candidate_id", "screenings", ["candidate_id"], unique=False)
    op.create_index(op.f("ix_screenings_tenant_id"), "screenings", ["tenant_id"], unique=False)
    op.execute(SEED_STAGES)
    op.execute(SEED_REASONS)
    op.get_bind().execute(sa.text(SEED_QUESTIONNAIRE), {"questions": json.dumps(DEFAULT_QUESTIONS)})
    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_rls(table)
        op.drop_table(table)
