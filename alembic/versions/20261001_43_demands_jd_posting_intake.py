# ruff: noqa: E501
"""JD versions, postings, biased terms and demand documents (stories 4.3/4.4) (tenant-owned, RLS).

Revision ID: 20261001_43
Revises: 20261001_42
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20261001_43"
down_revision: str | tuple[str, ...] | None = "20261001_42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = ("jd_versions", "demand_postings", "biased_terms", "demand_documents")


def upgrade() -> None:
    op.create_table(
        "jd_versions",
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("author_type", sa.String(length=10), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("ai_action_id", sa.Uuid(), nullable=True),
        sa.Column("restored_from_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("author_type IN ('user', 'ai')", name=op.f("ck_jd_versions_author_type")),
        sa.CheckConstraint(
            "kind IN ('full', 'short', 'posting', 'recruiter_summary', 'screening_questions', 'interview_questions', 'boolean')",
            name=op.f("ck_jd_versions_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name="fk_jd_versions_demand_id_demands",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_jd_versions_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jd_versions")),
        sa.UniqueConstraint("demand_id", "version_no", name=op.f("uq_jd_versions_demand_id_version_no")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_jd_versions_tenant_id_id")),
    )
    op.create_index(op.f("ix_jd_versions_tenant_id"), "jd_versions", ["tenant_id"], unique=False)
    op.create_table(
        "demand_postings",
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("jd_version_id", sa.Uuid(), nullable=False),
        sa.Column("show_rate", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("show_location", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("show_client_name", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_live", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unpublished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name="fk_demand_postings_demand_id_demands",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "jd_version_id"],
            ["jd_versions.tenant_id", "jd_versions.id"],
            name="fk_demand_postings_jd_version_id_jd_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_demand_postings_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demand_postings")),
        sa.UniqueConstraint("demand_id", name=op.f("uq_demand_postings_demand_id")),
    )
    op.create_index("ix_demand_postings_live", "demand_postings", ["is_live"], unique=False)
    op.create_index(op.f("ix_demand_postings_tenant_id"), "demand_postings", ["tenant_id"], unique=False)
    op.create_table(
        "biased_terms",
        sa.Column("term", postgresql.CITEXT(), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_biased_terms_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_biased_terms")),
        sa.UniqueConstraint("tenant_id", "term", name=op.f("uq_biased_terms_tenant_id_term")),
    )
    op.create_index(op.f("ix_biased_terms_tenant_id"), "biased_terms", ["tenant_id"], unique=False)
    op.create_table(
        "demand_documents",
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=200), nullable=False),
        sa.Column("scan_status", sa.String(length=20), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('intake_source', 'attachment')", name=op.f("ck_demand_documents_kind")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name="fk_demand_documents_demand_id_demands",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_demand_documents_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demand_documents")),
    )
    op.create_index("ix_demand_documents_demand_id", "demand_documents", ["demand_id"], unique=False)
    op.create_index(op.f("ix_demand_documents_tenant_id"), "demand_documents", ["tenant_id"], unique=False)
    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
