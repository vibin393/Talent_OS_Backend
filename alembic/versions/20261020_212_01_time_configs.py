# ruff: noqa: E501
"""Epic 25 (Timesheets) 212_01: ``time_settings``, ``timesheet_config_defaults`` and versioned
``timesheet_configs`` (FR-128).

``timesheet_configs.holiday_calendar_id`` has no FK here: the Leave branch (``p2_leave``, ``depends_on`` this
revision) adds ``timesheet_configs.holiday_calendar_id -> holiday_calendars.id``. Configs carry the placement's
org columns (copied by ``tos_inherit_org``) so worker / client sessions can read their own placement's config.

Revision ID: 20261020_212_01_time_configs
Revises: 20261002_200_07
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import InheritRef, disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261020_212_01_time_configs"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_timesheets",)
depends_on: str | Sequence[str] | None = None

ORG = ("client_id", "vendor_id", "candidate_id")
_CATEGORIES = "'{regular,ot,double_time,holiday,leave_paid,leave_unpaid}'::text[]"


def _config_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("period_kind", sa.String(12), server_default=sa.text("'weekly'"), nullable=False),
        sa.Column("week_start", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("entry_mode", sa.String(10), server_default=sa.text("'hours'"), nullable=False),
        sa.Column("hours_per_day", sa.Numeric(8, 2), server_default=sa.text("8.00"), nullable=False),
        sa.Column(
            "work_days",
            postgresql.ARRAY(sa.SmallInteger()),
            server_default=sa.text("'{0,1,2,3,4}'::smallint[]"),
            nullable=False,
        ),
        sa.Column(
            "enabled_categories",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text(_CATEGORIES),
            nullable=False,
        ),
        sa.Column("ot_daily_threshold", sa.Numeric(8, 2), nullable=True),
        sa.Column("dt_daily_threshold", sa.Numeric(8, 2), nullable=True),
        sa.Column("ot_weekly_threshold", sa.Numeric(8, 2), nullable=True),
        sa.Column(
            "pay_multipliers", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "bill_multipliers", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("daily_cap", sa.Numeric(8, 2), server_default=sa.text("24.00"), nullable=False),
        sa.Column("prefill_holidays", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("prefill_leave", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("holiday_calendar_id", sa.Uuid(), nullable=True),
        sa.Column(
            "approval_route", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
    ]


def _config_checks(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(
            "period_kind IN ('weekly', 'biweekly', 'semimonthly', 'monthly')", name=f"ck_{table}_period_kind"
        ),
        sa.CheckConstraint("entry_mode IN ('hours', 'start_end', 'days')", name=f"ck_{table}_entry_mode"),
        sa.CheckConstraint("week_start BETWEEN 0 AND 6", name=f"ck_{table}_week_start"),
        sa.CheckConstraint("hours_per_day > 0 AND hours_per_day <= 24", name=f"ck_{table}_hours_per_day"),
        sa.CheckConstraint("daily_cap > 0 AND daily_cap <= 24", name=f"ck_{table}_daily_cap"),
        sa.CheckConstraint(
            "(ot_daily_threshold IS NULL OR ot_daily_threshold >= 0) "
            "AND (dt_daily_threshold IS NULL OR dt_daily_threshold >= 0) "
            "AND (ot_weekly_threshold IS NULL OR ot_weekly_threshold >= 0)",
            name=f"ck_{table}_thresholds",
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "time_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("missing_grace_days", sa.Integer(), server_default=sa.text("2"), nullable=False),
        sa.Column(
            "reminder_offsets",
            postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{0,1}'::int[]"),
            nullable=False,
        ),
        sa.Column("copy_previous_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "import_allow_client_approved", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("allow_empty_route", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_time_settings_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_time_settings"),
    )
    op.create_index("ix_time_settings_tenant_id", "time_settings", ["tenant_id"])

    op.create_table(
        "timesheet_config_defaults",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        *_config_columns(),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        *_config_checks("timesheet_config_defaults"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_timesheet_config_defaults_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_timesheet_config_defaults_client_id_clients",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_timesheet_config_defaults"),
        sa.UniqueConstraint(
            "tenant_id", "client_id", name="uq_timesheet_config_defaults_tenant_id_client_id"
        ),
    )
    op.create_index("ix_timesheet_config_defaults_tenant_id", "timesheet_config_defaults", ["tenant_id"])

    op.create_table(
        "timesheet_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("effective_period_start", sa.Date(), nullable=False),
        *_config_columns(),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        *_config_checks("timesheet_configs"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_timesheet_configs_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "placement_id"],
            ["placements.tenant_id", "placements.id"],
            name="fk_timesheet_configs_placement_id_placements",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_timesheet_configs"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_timesheet_configs_tenant_id_id"),
        sa.UniqueConstraint("placement_id", "version", name="uq_timesheet_configs_placement_id_version"),
    )
    op.create_index("ix_timesheet_configs_tenant_id", "timesheet_configs", ["tenant_id"])
    op.create_index("ix_timesheet_configs_placement_id", "timesheet_configs", ["placement_id", "version"])

    for table in ("time_settings", "timesheet_config_defaults", "timesheet_configs"):
        enable_rls(table)
    enable_org_rls("time_settings")
    enable_org_rls("timesheet_config_defaults")
    enable_org_rls(
        "timesheet_configs",
        client_col="client_id",
        vendor_col="vendor_id",
        worker_col="candidate_id",
        inherit_from=InheritRef("placements", "placement_id", ORG),
    )


def downgrade() -> None:
    for table in ("timesheet_configs", "timesheet_config_defaults", "time_settings"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
