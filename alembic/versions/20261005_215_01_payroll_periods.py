# ruff: noqa: E501
"""Epic 28 (Payroll Export) 215_01: ``payroll_settings``, ``pay_calendars``, ``pay_periods`` (FR-140, AD-22).
``pay_periods`` carries an EXCLUDE gist constraint: one period per Legal Entity covers any date. Internal-only
tables (org RLS without org columns).

Revision ID: 20261005_215_01_payroll_periods
Revises: 20261002_200_07
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_215_01_payroll_periods"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_payroll",)
depends_on: str | Sequence[str] | None = None

_TABLES = ("payroll_settings", "pay_calendars", "pay_periods")


def _tenant(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
    )


def _ts() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist SCHEMA public")
    op.create_table(
        "payroll_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("late_item_mapping", sa.String(16), server_default=sa.text("'item_date'"), nullable=False),
        sa.Column(
            "include_worker_types",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{employee,contractor}'::text[]"),
            nullable=False,
        ),
        *_ts(),
        _tenant("payroll_settings"),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_payroll_settings"),
        sa.CheckConstraint(
            "late_item_mapping IN ('item_date', 'approval_date')",
            name=op.f("ck_payroll_settings_late_item_mapping"),
        ),
    )

    op.create_table(
        "pay_calendars",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("frequency", sa.String(10), server_default=sa.text("'monthly'"), nullable=False),
        sa.Column("cutoff_day", sa.SmallInteger(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_ts(),
        _tenant("pay_calendars"),
        sa.PrimaryKeyConstraint("id", name="pk_pay_calendars"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_pay_calendars_legal_entity_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "legal_entity_id", name="uq_pay_calendars_tenant_id_legal_entity_id"),
        sa.CheckConstraint("frequency = 'monthly'", name=op.f("ck_pay_calendars_frequency")),
        sa.CheckConstraint(
            "cutoff_day IS NULL OR cutoff_day BETWEEN 1 AND 28", name=op.f("ck_pay_calendars_cutoff_day")
        ),
    )
    op.create_index("ix_pay_calendars_tenant_id", "pay_calendars", ["tenant_id"])

    op.create_table(
        "pay_periods",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("human_id", sa.String(40), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'open'"), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.Uuid(), nullable=True),
        sa.Column("reopened_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_reopen_reason", sa.Text(), nullable=True),
        *_ts(),
        _tenant("pay_periods"),
        sa.PrimaryKeyConstraint("id", name="pk_pay_periods"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_pay_periods_legal_entity_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_pay_periods_tenant_id_id"),
        sa.CheckConstraint("status IN ('open', 'locked')", name=op.f("ck_pay_periods_status")),
        sa.CheckConstraint("period_end >= period_start", name=op.f("ck_pay_periods_period")),
    )
    op.create_index("ix_pay_periods_tenant_id", "pay_periods", ["tenant_id"])
    op.create_index("ix_pay_periods_le_start", "pay_periods", ["legal_entity_id", "period_start"])
    op.execute(
        "ALTER TABLE pay_periods ADD CONSTRAINT ex_pay_periods_no_overlap EXCLUDE USING gist "
        "(tenant_id WITH =, legal_entity_id WITH =, daterange(period_start, period_end, '[]') WITH &&)"
    )

    for table in _TABLES:
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
