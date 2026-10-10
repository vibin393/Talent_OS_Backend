# ruff: noqa: E501
"""Epic 26 (Leave & Holidays) 213_02: ``leave_settings``, ``leave_client_settings``, ``leave_types``,
``leave_type_client_overrides``, the append-only ``leave_balance_entries`` ledger (``tos_immutable`` without
mutable states; only ``reason`` may be cleared by erasure) and the ``leave_balances`` cache (FR-135).

Revision ID: 20261005_213_02_leave_types_bal
Revises: 20261005_213_01_holidays
Create Date: 2026-10-05
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    disable_immutability,
    disable_org_rls,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_rls,
)

revision: str = "20261005_213_02_leave_types_bal"
down_revision: str | tuple[str, ...] | None = "20261005_213_01_holidays"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tfk(table: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=f"fk_{table}_{column}_{target}"[:63],
        ondelete=ondelete,
    )


def _base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
    ]


def _ts() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


TABLES = (
    "leave_settings",
    "leave_client_settings",
    "leave_types",
    "leave_type_client_overrides",
    "leave_balance_entries",
    "leave_balances",
)


def upgrade() -> None:
    op.create_table(
        "leave_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("leave_year", sa.String(10), server_default=sa.text("'calendar'"), nullable=False),
        sa.Column(
            "default_work_week",
            postgresql.ARRAY(sa.SmallInteger()),
            server_default=sa.text("'{0,1,2,3,4}'::smallint[]"),
            nullable=False,
        ),
        sa.Column("accrual_day", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "client_team_calendar_default", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        *_ts(),
        sa.CheckConstraint("leave_year IN ('calendar', 'fiscal')", name="ck_leave_settings_leave_year"),
        sa.CheckConstraint("accrual_day BETWEEN 1 AND 28", name="ck_leave_settings_accrual_day"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_leave_settings_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_leave_settings"),
    )
    op.create_index("ix_leave_settings_tenant_id", "leave_settings", ["tenant_id"])

    op.create_table(
        "leave_client_settings",
        *_base("leave_client_settings"),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("leave_client_mode", sa.String(10), server_default=sa.text("'none'"), nullable=False),
        sa.Column("client_team_calendar", sa.Boolean(), nullable=True),
        *_ts(),
        sa.CheckConstraint(
            "leave_client_mode IN ('none', 'notify', 'approve')",
            name="ck_leave_client_settings_leave_client_mode",
        ),
        _tfk("leave_client_settings", "client_id", "clients", "CASCADE"),
        sa.UniqueConstraint("tenant_id", "client_id", name="uq_leave_client_settings_tenant_id_client_id"),
    )
    op.create_index("ix_leave_client_settings_tenant_id", "leave_client_settings", ["tenant_id"])

    op.create_table(
        "leave_types",
        *_base("leave_types"),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("paid", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("billable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("accrual_mode", sa.String(12), server_default=sa.text("'none'"), nullable=False),
        sa.Column("accrual_amount", sa.Numeric(6, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("cap", sa.Numeric(6, 2), nullable=True),
        sa.Column("carry_forward_limit", sa.Numeric(6, 2), nullable=True),
        sa.Column("allow_negative", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("negative_limit", sa.Numeric(6, 2), nullable=True),
        sa.Column("allow_half_day", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "eligible_worker_types",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "eligible_legal_entity_ids",
            postgresql.ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column(
            "timesheet_category", sa.String(20), server_default=sa.text("'leave_paid'"), nullable=False
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.CheckConstraint(
            "accrual_mode IN ('monthly', 'annual_grant', 'none')", name="ck_leave_types_accrual_mode"
        ),
        sa.CheckConstraint(
            "timesheet_category IN ('leave_paid', 'leave_unpaid')", name="ck_leave_types_timesheet_category"
        ),
        sa.CheckConstraint("accrual_amount >= 0", name="ck_leave_types_accrual_amount"),
        sa.CheckConstraint("cap IS NULL OR cap >= 0", name="ck_leave_types_cap"),
        sa.CheckConstraint(
            "carry_forward_limit IS NULL OR carry_forward_limit >= 0",
            name="ck_leave_types_carry_forward_limit",
        ),
        sa.CheckConstraint(
            "negative_limit IS NULL OR negative_limit >= 0", name="ck_leave_types_negative_limit"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_leave_types_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_leave_types_tenant_id_code"),
    )
    op.create_index("ix_leave_types_tenant_id", "leave_types", ["tenant_id"])

    op.create_table(
        "leave_type_client_overrides",
        *_base("leave_type_client_overrides"),
        sa.Column("leave_type_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("accrual_amount", sa.Numeric(6, 2), nullable=True),
        sa.Column("cap", sa.Numeric(6, 2), nullable=True),
        sa.Column("billable", sa.Boolean(), nullable=True),
        sa.Column("requires_client_approval", sa.Boolean(), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "leave_type_id"],
            ["leave_types.tenant_id", "leave_types.id"],
            name="fk_leave_type_client_overrides_leave_type_id",
            ondelete="CASCADE",
        ),
        _tfk("leave_type_client_overrides", "client_id", "clients", "CASCADE"),
        sa.UniqueConstraint("leave_type_id", "client_id", name="uq_leave_type_client_overrides_pair"),
    )
    op.create_index("ix_leave_type_client_overrides_tenant_id", "leave_type_client_overrides", ["tenant_id"])

    op.create_table(
        "leave_balance_entries",
        *_base("leave_balance_entries"),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("leave_type_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("leave_year", sa.String(10), nullable=False),
        sa.Column("entry_kind", sa.String(20), nullable=False),
        sa.Column("days", sa.Numeric(6, 2), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("source_ref", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor_kind", sa.String(20), server_default=sa.text("'system'"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "entry_kind IN ('accrual', 'grant', 'adjustment', 'request_debit', 'request_credit', 'carry_forward', 'lapse', 'opening')",
            name="ck_leave_balance_entries_entry_kind",
        ),
        sa.CheckConstraint("days <> 0", name="ck_leave_balance_entries_days"),
        _tfk("leave_balance_entries", "candidate_id", "candidates"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "leave_type_id"],
            ["leave_types.tenant_id", "leave_types.id"],
            name="fk_leave_balance_entries_leave_type_id_leave_types",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_leave_balance_entries_legal_entity_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "leave_type_id",
            "legal_entity_id",
            "source_ref",
            name="uq_leave_balance_entries_source",
        ),
    )
    op.create_index("ix_leave_balance_entries_tenant_id", "leave_balance_entries", ["tenant_id"])
    op.create_index(
        "ix_leave_balance_entries_key",
        "leave_balance_entries",
        ["candidate_id", "leave_type_id", "legal_entity_id", "leave_year"],
    )

    op.create_table(
        "leave_balances",
        *_base("leave_balances"),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("leave_type_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("leave_year", sa.String(10), nullable=False),
        sa.Column("balance", sa.Numeric(6, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("pending", sa.Numeric(6, 2), server_default=sa.text("0"), nullable=False),
        *_ts(),
        sa.CheckConstraint("pending >= 0", name="ck_leave_balances_pending"),
        _tfk("leave_balances", "candidate_id", "candidates"),
        _tfk("leave_balances", "leave_type_id", "leave_types"),
        _tfk("leave_balances", "legal_entity_id", "legal_entities"),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "leave_type_id",
            "legal_entity_id",
            "leave_year",
            name="uq_leave_balances_key",
        ),
    )
    op.create_index("ix_leave_balances_tenant_id", "leave_balances", ["tenant_id"])
    op.create_index("ix_leave_balances_candidate_id", "leave_balances", ["candidate_id"])

    for table in TABLES:
        enable_rls(table)
    enable_org_rls("leave_settings")
    enable_org_rls("leave_client_settings", client_col="client_id")
    enable_org_rls("leave_types")
    enable_org_rls("leave_type_client_overrides")
    enable_org_rls("leave_balance_entries", worker_col="candidate_id")
    enable_org_rls("leave_balances", worker_col="candidate_id")
    enable_immutability(
        "leave_balance_entries", status_column=None, mutable_states=(), allowed_columns=("reason",)
    )


def downgrade() -> None:
    disable_immutability("leave_balance_entries")
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
