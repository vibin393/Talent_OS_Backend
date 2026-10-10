"""Duplicates, merges and bulk import (stories 3.5 / 3.6) (tenant-owned, RLS).

Revision ID: 20261001_34
Revises: 20261001_33
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20261001_34"
down_revision: str | None = "20261001_33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candidate_duplicates",
        sa.Column("candidate_a_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_b_id", sa.Uuid(), nullable=False),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("score", sa.Numeric(precision=5, scale=3), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'merged', 'kept_separate')", name=op.f("ck_candidate_duplicates_status")
        ),
        sa.CheckConstraint(
            "candidate_a_id < candidate_b_id", name=op.f("ck_candidate_duplicates_ordered_pair")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_a_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_duplicates_candidate_a_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_b_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_duplicates_candidate_b_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_candidate_duplicates_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_duplicates")),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_a_id",
            "candidate_b_id",
            name=op.f("uq_candidate_duplicates_tenant_id_candidate_a_id_candidate_b_id"),
        ),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_candidate_duplicates_tenant_id_id")),
    )
    op.create_index(
        "ix_candidate_duplicates_candidate_b_id", "candidate_duplicates", ["candidate_b_id"], unique=False
    )
    op.create_index(
        op.f("ix_candidate_duplicates_tenant_id"), "candidate_duplicates", ["tenant_id"], unique=False
    )
    enable_rls("candidate_duplicates")

    op.create_table(
        "candidate_merges",
        sa.Column("survivor_id", sa.Uuid(), nullable=False),
        sa.Column("merged_id", sa.Uuid(), nullable=False),
        sa.Column("duplicate_id", sa.Uuid(), nullable=True),
        sa.Column("field_choices", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("merged_by", sa.Uuid(), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("undo_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "duplicate_id"],
            ["candidate_duplicates.tenant_id", "candidate_duplicates.id"],
            name="fk_candidate_merges_duplicate_id_candidate_duplicates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merged_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_merges_merged_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "survivor_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_merges_survivor_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_candidate_merges_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_merges")),
    )
    op.create_index("ix_candidate_merges_merged_id", "candidate_merges", ["merged_id"], unique=False)
    op.create_index("ix_candidate_merges_survivor_id", "candidate_merges", ["survivor_id"], unique=False)
    op.create_index(op.f("ix_candidate_merges_tenant_id"), "candidate_merges", ["tenant_id"], unique=False)
    enable_rls("candidate_merges")

    op.create_table(
        "import_batches",
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'completed')", name=op.f("ck_import_batches_status")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_import_batches_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_batches")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_import_batches_tenant_id_id")),
    )
    op.create_index(op.f("ix_import_batches_tenant_id"), "import_batches", ["tenant_id"], unique=False)
    enable_rls("import_batches")

    op.create_table(
        "import_items",
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("duplicate_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'created', 'linked', 'needs_review', 'failed')",
            name=op.f("ck_import_items_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "batch_id"],
            ["import_batches.tenant_id", "import_batches.id"],
            name="fk_import_items_batch_id_import_batches",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_import_items_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "duplicate_id"],
            ["candidate_duplicates.tenant_id", "candidate_duplicates.id"],
            name="fk_import_items_duplicate_id_candidate_duplicates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "file_id"],
            ["stored_files.tenant_id", "stored_files.id"],
            name="fk_import_items_file_id_stored_files",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_import_items_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_items")),
    )
    op.create_index("ix_import_items_batch_id", "import_items", ["batch_id"], unique=False)
    op.create_index(op.f("ix_import_items_tenant_id"), "import_items", ["tenant_id"], unique=False)
    enable_rls("import_items")


def downgrade() -> None:
    op.drop_table("import_items")
    op.drop_table("import_batches")
    op.drop_table("candidate_merges")
    op.drop_table("candidate_duplicates")
