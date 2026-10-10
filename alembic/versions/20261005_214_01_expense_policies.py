"""Epic 27 (Expenses) 214_01: ``expense_settings``, ``expense_policies``, ``expense_policy_categories``
(FR-137). Internal-only tables (org RLS without org columns).

Revision ID: 20261005_214_01_expense_policies
Revises: 20261002_200_07
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_214_01_expense_policies"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_expenses",)
depends_on: str | Sequence[str] | None = None

_CATEGORIES = "category IN ('travel', 'accommodation', 'meals', 'mileage', 'equipment', 'other')"
_TABLES = ("expense_settings", "expense_policies", "expense_policy_categories")


def _tenant(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
    )


def upgrade() -> None:
    op.create_table(
        "expense_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("ai_receipt_read_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "duplicate_receipt_window_days", sa.Integer(), server_default=sa.text("90"), nullable=False
        ),
        sa.Column("max_receipt_mb", sa.Integer(), server_default=sa.text("10"), nullable=False),
        sa.Column(
            "allowed_receipt_types",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{image/jpeg,image/png,image/heic,application/pdf}'::text[]"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _tenant("expense_settings"),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_expense_settings"),
    )

    op.create_table(
        "expense_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("scope", sa.String(10), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("scope IN ('tenant', 'client')", name="ck_expense_policies_scope"),
        sa.CheckConstraint(
            "(scope = 'tenant' AND client_id IS NULL) OR (scope = 'client' AND client_id IS NOT NULL)",
            name="ck_expense_policies_scope_client",
        ),
        sa.CheckConstraint("version >= 1", name="ck_expense_policies_version"),
        _tenant("expense_policies"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_expense_policies_client_id_clients",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_expense_policies"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_expense_policies_tenant_id_id"),
    )
    op.create_index("ix_expense_policies_tenant_id", "expense_policies", ["tenant_id"])
    op.create_index(
        "uq_expense_policies_tenant_version",
        "expense_policies",
        ["tenant_id", "version"],
        unique=True,
        postgresql_where=sa.text("scope = 'tenant'"),
    )
    op.create_index(
        "uq_expense_policies_client_version",
        "expense_policies",
        ["tenant_id", "client_id", "version"],
        unique=True,
        postgresql_where=sa.text("scope = 'client'"),
    )

    op.create_table(
        "expense_policy_categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("daily_limit", sa.Numeric(16, 2), nullable=True),
        sa.Column("claim_limit", sa.Numeric(16, 2), nullable=True),
        sa.Column("limit_currency", sa.CHAR(3), server_default=sa.text("'INR'"), nullable=False),
        sa.Column("receipt_required_above", sa.Numeric(16, 2), nullable=True),
        sa.Column("mileage_rate_per_km", sa.Numeric(14, 4), nullable=True),
        sa.Column("default_billable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("rebill_method", sa.String(20), server_default=sa.text("'at_cost'"), nullable=False),
        sa.Column("markup_pct", sa.Numeric(7, 4), nullable=True),
        sa.Column("rebill_tax_mode", sa.String(20), server_default=sa.text("'taxable'"), nullable=False),
        sa.CheckConstraint(_CATEGORIES, name="ck_expense_policy_categories_category"),
        sa.CheckConstraint(
            "rebill_method IN ('at_cost', 'markup', 'not_billable')",
            name="ck_expense_policy_categories_rebill_method",
        ),
        sa.CheckConstraint(
            "rebill_tax_mode IN ('taxable', 'pure_agent')",
            name="ck_expense_policy_categories_rebill_tax_mode",
        ),
        sa.CheckConstraint(
            "(daily_limit IS NULL OR daily_limit >= 0) AND (claim_limit IS NULL OR claim_limit >= 0) "
            "AND (receipt_required_above IS NULL OR receipt_required_above >= 0) "
            "AND (mileage_rate_per_km IS NULL OR mileage_rate_per_km >= 0) "
            "AND (markup_pct IS NULL OR markup_pct >= 0)",
            name="ck_expense_policy_categories_non_negative",
        ),
        _tenant("expense_policy_categories"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "policy_id"],
            ["expense_policies.tenant_id", "expense_policies.id"],
            name="fk_expense_policy_categories_policy_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_expense_policy_categories"),
        sa.UniqueConstraint("policy_id", "category", name="uq_expense_policy_categories_policy_id_category"),
    )
    op.create_index("ix_expense_policy_categories_tenant_id", "expense_policy_categories", ["tenant_id"])
    op.create_index("ix_expense_policy_categories_policy_id", "expense_policy_categories", ["policy_id"])

    for table in _TABLES:
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
