"""Epic 6 submissions: submissions, candidate/client firsts (FR-24), client feedback (tenant-owned, RLS).

Revision ID: 20261001_62
Revises: 20261001_61
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_62"
down_revision: str | tuple[str, ...] | None = "20261001_61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = ("submissions", "candidate_client_firsts", "client_feedback")


def upgrade() -> None:
    op.create_table(
        "candidate_client_firsts",
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("first_submitted_by", sa.Uuid(), nullable=True),
        sa.Column("first_submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_client_firsts_candidate_id_candidates",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_candidate_client_firsts_client_id_clients",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_candidate_client_firsts_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_client_firsts")),
        sa.UniqueConstraint(
            "tenant_id",
            "client_id",
            "candidate_id",
            name=op.f("uq_candidate_client_firsts_tenant_id_client_id_candidate_id"),
        ),
    )
    op.create_index(
        op.f("ix_candidate_client_firsts_tenant_id"), "candidate_client_firsts", ["tenant_id"], unique=False
    )
    op.create_table(
        "submissions",
        sa.Column("human_id", sa.String(length=20), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("pay_rate", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("bill_rate", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("rate_unit", sa.String(length=10), nullable=False),
        sa.Column("employment_type", sa.String(length=20), nullable=True),
        sa.Column("burden_pct", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("margin_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("margin_pct", sa.Numeric(precision=7, scale=2), nullable=False),
        sa.Column("below_min_margin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("resume_kind", sa.String(length=20), nullable=False),
        sa.Column("resume_file_id", sa.Uuid(), nullable=True),
        sa.Column("resume_sha256", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "recipient_contact_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "cc_user_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("email_subject", sa.String(length=300), nullable=True),
        sa.Column("email_body", sa.Text(), nullable=True),
        sa.Column(
            "email_message_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("prior_submission_reason", sa.Text(), nullable=True),
        sa.Column("consent_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_by", sa.Uuid(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("feedback_status", sa.String(length=20), server_default=sa.text("'none'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "feedback_status IN ('none', 'interested', 'rejected_pending', 'rejected', 'on_hold')",
            name=op.f("ck_submissions_feedback_status"),
        ),
        sa.CheckConstraint(
            "rate_unit IN ('hour', 'day', 'month', 'year')", name=op.f("ck_submissions_rate_unit")
        ),
        sa.CheckConstraint(
            "resume_kind IN ('original', 'client_formatted')", name=op.f("ck_submissions_resume_kind")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'approved', 'sent', 'cancelled', 'approval_rejected')",
            name=op.f("ck_submissions_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            name="fk_submissions_application_id_applications",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_submissions_candidate_id_candidates",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_submissions_client_id_clients",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name="fk_submissions_demand_id_demands",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_submissions_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_submissions")),
        sa.UniqueConstraint("tenant_id", "human_id", name=op.f("uq_submissions_tenant_id_human_id")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_submissions_tenant_id_id")),
    )
    op.create_index("ix_submissions_application_id", "submissions", ["application_id"], unique=False)
    op.create_index(
        "ix_submissions_candidate_client_sent",
        "submissions",
        ["candidate_id", "client_id", "sent_at"],
        unique=False,
    )
    op.create_index("ix_submissions_demand_id_status", "submissions", ["demand_id", "status"], unique=False)
    op.create_index(op.f("ix_submissions_tenant_id"), "submissions", ["tenant_id"], unique=False)
    op.create_table(
        "client_feedback",
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("recorded_by", sa.Uuid(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_by", sa.Uuid(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_by", sa.Uuid(), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "decision IN ('interested', 'rejected', 'on_hold')", name=op.f("ck_client_feedback_decision")
        ),
        sa.CheckConstraint("source IN ('link', 'recorded')", name=op.f("ck_client_feedback_source")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "submission_id"],
            ["submissions.tenant_id", "submissions.id"],
            name="fk_client_feedback_submission_id_submissions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_client_feedback_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_feedback")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_client_feedback_tenant_id_id")),
    )
    op.create_index("ix_client_feedback_application_id", "client_feedback", ["application_id"], unique=False)
    op.create_index("ix_client_feedback_submission_id", "client_feedback", ["submission_id"], unique=False)
    op.create_index(op.f("ix_client_feedback_tenant_id"), "client_feedback", ["tenant_id"], unique=False)
    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_rls(table)
        op.drop_table(table)
