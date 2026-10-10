# ruff: noqa: E501, RUF028
"""Epic 67 (Public API & OAuth Apps, stories 67.5 / 67.6, spec A2): ``public_import_jobs`` (the public wrapper around a
P1 bulk import batch) and ``api_deprecation_notices`` (monthly deprecated-call report). Tenant tables with RLS + org RLS.

Revision ID: 20261010_501_01_public_api
Revises: 20261009_500_06_p5_vault_drop
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261010_501_01_public_api"
down_revision: str | tuple[str, ...] | None = "20261009_500_06_p5_vault_drop"
branch_labels: str | Sequence[str] | None = ("p5_public_api",)
depends_on: str | Sequence[str] | None = None

TABLES = ("public_import_jobs", "api_deprecation_notices")


def _base(t: str) -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT", name=f"fk_{t}_tenant_id_tenants"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=True),
        sa.Column("api_key_id", sa.Uuid(), nullable=True),
    ]


def upgrade() -> None:
    t = "public_import_jobs"
    op.create_table(
        t,
        *_base(t),
        sa.Column("acting_user_id", sa.Uuid(), nullable=True),
        sa.Column("resource", sa.String(20), nullable=False),
        sa.Column("format", sa.String(10), nullable=False),
        sa.Column("file_key", sa.String(300), nullable=False),
        sa.Column("row_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(12), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("counts", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("error_report_key", sa.String(300), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.CheckConstraint("resource IN ('candidates', 'applications')", name=op.f("ck_public_import_jobs_resource")),
        sa.CheckConstraint("format IN ('csv', 'jsonl')", name=op.f("ck_public_import_jobs_format")),
        sa.CheckConstraint(
            "status IN ('queued', 'validating', 'running', 'completed', 'failed', 'cancelled')", name=op.f("ck_public_import_jobs_status")
        ),
        sa.CheckConstraint("installation_id IS NOT NULL OR api_key_id IS NOT NULL", name=op.f("ck_public_import_jobs_owner")),
    )
    op.create_index("ix_public_import_jobs_inst_created", t, ["tenant_id", "installation_id", sa.text("created_at DESC")])
    op.create_index("ix_public_import_jobs_batch_id", t, ["batch_id"])

    t = "api_deprecation_notices"
    op.create_table(
        t,
        *_base(t),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("route_template", sa.String(300), nullable=False),
        sa.Column("call_count", sa.BigInteger(), nullable=False),
        sa.Column("sunset", sa.Date(), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "installation_id", "api_key_id", "month", "route_template",
            name="uq_api_deprecation_notices_key", postgresql_nulls_not_distinct=True,
        ),  # fmt: skip
        sa.CheckConstraint("installation_id IS NOT NULL OR api_key_id IS NOT NULL", name=op.f("ck_api_deprecation_notices_owner")),
    )
    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
