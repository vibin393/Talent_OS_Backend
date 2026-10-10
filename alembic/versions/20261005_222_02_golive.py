# ruff: noqa: E501
"""Epic 35 222_02: ``golive_import_rows`` (FR-185) on the foundation ``file_import_batches``.

Tenant RLS + internal-only org RLS (go-live import is an internal admin tool).

Revision ID: 20261005_222_02_golive
Revises: 20261005_222_01_privacy_retain
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_222_02_golive"
down_revision: str | tuple[str, ...] | None = "20261005_222_01_privacy_retain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "golive_import_rows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("errors", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("result_ref", sa.String(80), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_golive_import_rows")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_golive_import_rows_tenant_id_tenants"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["batch_id"], ["file_import_batches.id"], name="fk_golive_import_rows_batch_id_file_import_batches", ondelete="CASCADE"),
        sa.UniqueConstraint("batch_id", "row_no", name=op.f("uq_golive_import_rows_batch_id_row_no")),
        sa.CheckConstraint("status IN ('valid', 'invalid', 'applied', 'failed', 'skipped')", name=op.f("ck_golive_import_rows_status")),
    )
    op.create_index(op.f("ix_golive_import_rows_tenant_id"), "golive_import_rows", ["tenant_id"])
    op.create_index("ix_golive_import_rows_batch_status", "golive_import_rows", ["batch_id", "status"])
    enable_rls("golive_import_rows")
    enable_org_rls("golive_import_rows")


def downgrade() -> None:
    disable_org_rls("golive_import_rows")
    disable_rls("golive_import_rows")
    op.drop_index("ix_golive_import_rows_batch_status", table_name="golive_import_rows")
    op.drop_index(op.f("ix_golive_import_rows_tenant_id"), table_name="golive_import_rows")
    op.drop_table("golive_import_rows")
