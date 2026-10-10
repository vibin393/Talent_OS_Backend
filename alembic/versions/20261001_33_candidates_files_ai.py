"""Files, AI gateway and resumes (stories 3.3 / 3.4): ``stored_files``, ``attachments``, ``ai_actions``,
``ai_tenant_settings``, ``candidate_resumes`` (tenant-owned, RLS).

Revision ID: 20261001_33
Revises: 20261001_32
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20261001_33"
down_revision: str | None = "20261001_32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stored_files",
        sa.Column("storage_key", sa.String(length=200), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("scan_status", sa.String(length=20), nullable=False),
        sa.Column("owner_entity_type", sa.String(length=50), nullable=True),
        sa.Column("owner_entity_id", sa.Uuid(), nullable=True),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "scan_status IN ('clean', 'infected', 'skipped_dev', 'error')",
            name=op.f("ck_stored_files_scan_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_stored_files_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stored_files")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_stored_files_storage_key")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_stored_files_tenant_id_id")),
    )
    op.create_index(
        "ix_stored_files_owner", "stored_files", ["owner_entity_type", "owner_entity_id"], unique=False
    )
    op.create_index(op.f("ix_stored_files_tenant_id"), "stored_files", ["tenant_id"], unique=False)
    enable_rls("stored_files")

    op.create_table(
        "attachments",
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "category IN ('certificate', 'id_proof', 'offer_letter', 'relieving_letter', 'payslip', 'other')",
            name=op.f("ck_attachments_category"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "file_id"],
            ["stored_files.tenant_id", "stored_files.id"],
            name="fk_attachments_file_id_stored_files",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_attachments_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attachments")),
    )
    op.create_index("ix_attachments_entity", "attachments", ["entity_type", "entity_id"], unique=False)
    op.create_index(op.f("ix_attachments_tenant_id"), "attachments", ["tenant_id"], unique=False)
    enable_rls("attachments")

    op.create_table(
        "ai_actions",
        sa.Column("feature", sa.String(length=50), nullable=False),
        sa.Column("prompt_id", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=120), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("initiator_type", sa.String(length=20), nullable=False),
        sa.Column("initiator_id", sa.Uuid(), nullable=True),
        sa.Column("subject_type", sa.String(length=50), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("input_ref", sa.String(length=200), nullable=True),
        sa.Column("input_sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("limitations_note", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("consent_basis", sa.String(length=50), nullable=True),
        sa.Column("decision", sa.String(length=20), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('accepted', 'edited', 'rejected')",
            name=op.f("ck_ai_actions_decision"),
        ),
        sa.CheckConstraint("status IN ('ok', 'failed', 'skipped')", name=op.f("ck_ai_actions_status")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_ai_actions_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_actions")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_ai_actions_tenant_id_id")),
    )
    op.create_index("ix_ai_actions_subject", "ai_actions", ["subject_type", "subject_id"], unique=False)
    op.create_index(op.f("ix_ai_actions_tenant_id"), "ai_actions", ["tenant_id"], unique=False)
    op.create_index(
        "ix_ai_actions_tenant_id_created_at", "ai_actions", ["tenant_id", "created_at"], unique=False
    )
    enable_rls("ai_actions")

    op.create_table(
        "ai_tenant_settings",
        sa.Column(
            "features",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("monthly_token_budget", sa.Integer(), nullable=True),
        sa.Column(
            "ai_processing_basis",
            sa.String(length=30),
            server_default=sa.text("'consent_required'"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "ai_processing_basis IN ('consent_required', 'notice_based')",
            name=op.f("ck_ai_tenant_settings_ai_processing_basis"),
        ),
        sa.CheckConstraint(
            "monthly_token_budget IS NULL OR monthly_token_budget >= 0",
            name=op.f("ck_ai_tenant_settings_budget"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_ai_tenant_settings_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_tenant_settings")),
        sa.UniqueConstraint("tenant_id", name=op.f("uq_ai_tenant_settings_tenant_id")),
    )
    op.create_index(
        op.f("ix_ai_tenant_settings_tenant_id"), "ai_tenant_settings", ["tenant_id"], unique=False
    )
    enable_rls("ai_tenant_settings")

    op.create_table(
        "candidate_resumes",
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column(
            "text_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple'::regconfig, coalesce(extracted_text, ''))", persisted=True),
            nullable=True,
        ),
        sa.Column("text_status", sa.String(length=20), nullable=False),
        sa.Column("parse_status", sa.String(length=20), nullable=False),
        sa.Column("ai_action_id", sa.Uuid(), nullable=True),
        sa.Column("parsed_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "parse_status IN ('parsed', 'failed_manual', 'unavailable', 'skipped_disabled', 'skipped_consent')",
            name=op.f("ck_candidate_resumes_parse_status"),
        ),
        sa.CheckConstraint(
            "text_status IN ('ok', 'image_only', 'encrypted', 'failed')",
            name=op.f("ck_candidate_resumes_text_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_resumes_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "file_id"],
            ["stored_files.tenant_id", "stored_files.id"],
            name="fk_candidate_resumes_file_id_stored_files",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_candidate_resumes_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_resumes")),
        sa.UniqueConstraint(
            "candidate_id", "version", name=op.f("uq_candidate_resumes_candidate_id_version")
        ),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_candidate_resumes_tenant_id_id")),
    )
    op.create_index("ix_candidate_resumes_candidate_id", "candidate_resumes", ["candidate_id"], unique=False)
    op.create_index(op.f("ix_candidate_resumes_tenant_id"), "candidate_resumes", ["tenant_id"], unique=False)
    op.create_index(
        "ix_candidate_resumes_text_tsv",
        "candidate_resumes",
        ["text_tsv"],
        unique=False,
        postgresql_using="gin",
    )
    enable_rls("candidate_resumes")


def downgrade() -> None:
    op.drop_table("candidate_resumes")
    op.drop_table("ai_tenant_settings")
    op.drop_table("ai_actions")
    op.drop_table("attachments")
    op.drop_table("stored_files")
