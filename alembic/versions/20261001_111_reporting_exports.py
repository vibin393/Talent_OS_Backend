# ruff: noqa: E501
"""Reporting: ``exports`` (FR-61) and ``user_active_days`` (SM-3) (Epic 11, stories 11.1 / 11.2) (tenant-owned,
RLS).

Revision ID: 20261001_111
Revises: 20261001_81
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20261001_111"
# The head at implementation time (Epic 8 offers); files (33), demands (41), applications (61) and Epic 10's
# 101 are all ancestors of it, so no extra ``depends_on`` is needed (declaring ancestors confuses
# ``upgrade heads``).
down_revision: str | tuple[str, ...] | None = "20261001_81"
branch_labels: str | Sequence[str] | None = ("reporting",)
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = ("exports", "user_active_days")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "exports",
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("format", sa.String(length=5), nullable=False),
        sa.Column("mode", sa.String(length=5), nullable=False),
        sa.Column(
            "filters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "columns",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "masked_columns",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            _in("kind", ("candidates", "demands", "applications", "submissions", "interviews", "offers")),
            name=op.f("ck_exports_kind"),
        ),
        sa.CheckConstraint(_in("format", ("csv", "xlsx")), name=op.f("ck_exports_format")),
        sa.CheckConstraint(
            _in("status", ("queued", "running", "done", "failed", "expired")), name=op.f("ck_exports_status")
        ),
        sa.CheckConstraint(_in("mode", ("sync", "async")), name=op.f("ck_exports_mode")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "requested_by"],
            ["users.tenant_id", "users.id"],
            name="fk_exports_requested_by_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_exports_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exports")),
    )
    op.create_index(op.f("ix_exports_tenant_id"), "exports", ["tenant_id"], unique=False)
    op.create_index(
        "ix_exports_requested_by_created",
        "exports",
        ["tenant_id", "requested_by", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_exports_expiry",
        "exports",
        ["tenant_id", "expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'done'"),
    )

    op.create_table(
        "user_active_days",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_user_active_days_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_user_active_days_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", "day", name=op.f("pk_user_active_days")),
    )
    op.create_index("ix_user_active_days_day", "user_active_days", ["tenant_id", "day"], unique=False)

    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
