"""Epic 9 self-service privacy: ``data_export_jobs`` (tenant-owned, RLS) and the permission seed
(``career_site:configure`` and ``dsr:read`` on every tenant's Tenant Admin role).

The DSR intake uses Epic 12's ``dsr_requests`` table (migration 20261001_121) -- no second DSR table.
The system Tenant Admin role always confers every catalog permission (``effective_role_grants``); the seed keeps
the stored JSON in step for reporting / exports.

Revision ID: 20261001_92
Revises: 20261001_91
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_92"
down_revision: str | tuple[str, ...] | None = "20261001_91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED = '{"career_site:configure": "tenant", "dsr:read": "tenant"}'


def upgrade() -> None:
    op.create_table(
        "data_export_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("storage_key", sa.String(length=300), nullable=True),
        sa.Column("secure_link_id", sa.Uuid(), nullable=True),
        sa.Column("dsr_id", sa.Uuid(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'ready', 'failed', 'expired')",
            name=op.f("ck_data_export_jobs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_data_export_jobs_candidate_id_candidates",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_data_export_jobs_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_export_jobs")),
    )
    op.create_index(op.f("ix_data_export_jobs_tenant_id"), "data_export_jobs", ["tenant_id"], unique=False)
    op.create_index("ix_data_export_jobs_candidate_id", "data_export_jobs", ["candidate_id"], unique=False)
    op.create_index("ix_data_export_jobs_status", "data_export_jobs", ["status"], unique=False)
    enable_rls("data_export_jobs")

    op.execute(f"UPDATE roles SET permissions = permissions || '{SEED}'::jsonb WHERE key = 'tenant_admin'")


def downgrade() -> None:
    op.execute(
        "UPDATE roles SET permissions = permissions - 'career_site:configure' WHERE key = 'tenant_admin'"
    )
    disable_rls("data_export_jobs")
    op.drop_table("data_export_jobs")
