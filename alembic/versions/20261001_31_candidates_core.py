"""Candidates core (story 3.1): ``candidates`` + contact / history child tables (tenant-owned, RLS).

Revision ID: 20261001_31
Revises: 20260930_06
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20261001_31"
down_revision: str | None = "20260930_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candidates",
        sa.Column("human_id", sa.String(length=20), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("location", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("current_title", sa.String(length=200), nullable=True),
        sa.Column("current_employer", sa.String(length=200), nullable=True),
        sa.Column("total_experience_months", sa.Integer(), nullable=True),
        sa.Column("current_ctc_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("current_ctc_currency", sa.String(length=3), nullable=True),
        sa.Column("current_ctc_unit", sa.String(length=10), nullable=True),
        sa.Column("expected_ctc_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("expected_ctc_currency", sa.String(length=3), nullable=True),
        sa.Column("expected_ctc_unit", sa.String(length=10), nullable=True),
        sa.Column("availability_date", sa.Date(), nullable=True),
        sa.Column("notice_period_days", sa.Integer(), nullable=True),
        sa.Column("last_working_date", sa.Date(), nullable=True),
        sa.Column("work_authorization", sa.String(length=200), nullable=True),
        sa.Column("willing_to_relocate", sa.Boolean(), nullable=True),
        sa.Column("preferred_locations", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("source_detail", sa.String(length=500), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_into_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "current_ctc_unit IS NULL OR current_ctc_unit IN ('annual', 'monthly', 'daily', 'hourly')",
            name=op.f("ck_candidates_cur_unit"),
        ),
        sa.CheckConstraint(
            "expected_ctc_unit IS NULL OR expected_ctc_unit IN ('annual', 'monthly', 'daily', 'hourly')",
            name=op.f("ck_candidates_exp_unit"),
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'resume_upload', 'bulk_import', 'career_portal', 'referral', 'job_board', 'other')",
            name=op.f("ck_candidates_source"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merged_into_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidates_merged_into_id_candidates",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidates_owner_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_candidates_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidates")),
        sa.UniqueConstraint("tenant_id", "human_id", name=op.f("uq_candidates_tenant_id_human_id")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_candidates_tenant_id_id")),
    )
    op.create_index(
        "ix_candidates_current_employer_trgm",
        "candidates",
        ["current_employer"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"current_employer": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_candidates_full_name_trgm",
        "candidates",
        ["full_name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"full_name": "gin_trgm_ops"},
    )
    op.create_index("ix_candidates_owner_id", "candidates", ["owner_id"], unique=False)
    op.create_index(op.f("ix_candidates_tenant_id"), "candidates", ["tenant_id"], unique=False)
    enable_rls("candidates")

    op.create_table(
        "candidate_emails",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_emails_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_candidate_emails_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_emails")),
    )
    op.create_index("ix_candidate_emails_candidate_id", "candidate_emails", ["candidate_id"], unique=False)
    op.create_index(op.f("ix_candidate_emails_tenant_id"), "candidate_emails", ["tenant_id"], unique=False)
    op.create_index(
        "ix_candidate_emails_tenant_id_email", "candidate_emails", ["tenant_id", "email"], unique=False
    )
    enable_rls("candidate_emails")

    op.create_table(
        "candidate_phones",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("phone_e164", sa.String(length=20), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_phones_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_candidate_phones_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_phones")),
    )
    op.create_index("ix_candidate_phones_candidate_id", "candidate_phones", ["candidate_id"], unique=False)
    op.create_index(op.f("ix_candidate_phones_tenant_id"), "candidate_phones", ["tenant_id"], unique=False)
    op.create_index(
        "ix_candidate_phones_tenant_id_phone_e164",
        "candidate_phones",
        ["tenant_id", "phone_e164"],
        unique=False,
    )
    enable_rls("candidate_phones")

    op.create_table(
        "candidate_links",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("url_norm", sa.String(length=500), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('linkedin', 'github', 'portfolio', 'other')", name=op.f("ck_candidate_links_kind")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_links_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_candidate_links_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_links")),
    )
    op.create_index("ix_candidate_links_candidate_id", "candidate_links", ["candidate_id"], unique=False)
    op.create_index(op.f("ix_candidate_links_tenant_id"), "candidate_links", ["tenant_id"], unique=False)
    op.create_index(
        "ix_candidate_links_tenant_id_url_norm", "candidate_links", ["tenant_id", "url_norm"], unique=False
    )
    enable_rls("candidate_links")

    op.create_table(
        "candidate_employments",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("employer", sa.String(length=200), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("location", sa.String(length=200), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("is_current", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_employments_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_candidate_employments_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_employments")),
    )
    op.create_index(
        "ix_candidate_employments_candidate_id", "candidate_employments", ["candidate_id"], unique=False
    )
    op.create_index(
        op.f("ix_candidate_employments_tenant_id"), "candidate_employments", ["tenant_id"], unique=False
    )
    enable_rls("candidate_employments")

    op.create_table(
        "candidate_educations",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("institution", sa.String(length=300), nullable=True),
        sa.Column("degree", sa.String(length=200), nullable=True),
        sa.Column("field", sa.String(length=200), nullable=True),
        sa.Column("start_year", sa.Integer(), nullable=True),
        sa.Column("end_year", sa.Integer(), nullable=True),
        sa.Column("grade", sa.String(length=50), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_educations_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_candidate_educations_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_educations")),
    )
    op.create_index(
        "ix_candidate_educations_candidate_id", "candidate_educations", ["candidate_id"], unique=False
    )
    op.create_index(
        op.f("ix_candidate_educations_tenant_id"), "candidate_educations", ["tenant_id"], unique=False
    )
    enable_rls("candidate_educations")

    op.create_table(
        "candidate_certifications",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("issuer", sa.String(length=200), nullable=True),
        sa.Column("issued_on", sa.Date(), nullable=True),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("credential_id", sa.String(length=200), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_certifications_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_candidate_certifications_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_certifications")),
    )
    op.create_index(
        "ix_candidate_certifications_candidate_id", "candidate_certifications", ["candidate_id"], unique=False
    )
    op.create_index(
        op.f("ix_candidate_certifications_tenant_id"), "candidate_certifications", ["tenant_id"], unique=False
    )
    enable_rls("candidate_certifications")


def downgrade() -> None:
    op.drop_table("candidate_certifications")
    op.drop_table("candidate_educations")
    op.drop_table("candidate_employments")
    op.drop_table("candidate_links")
    op.drop_table("candidate_phones")
    op.drop_table("candidate_emails")
    op.drop_table("candidates")
