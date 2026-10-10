# ruff: noqa: E501
"""Epic 26 (Leave & Holidays) 213_01: ``holiday_calendars``, ``holidays``, ``worker_optional_holidays`` (FR-134)
and the FK ``timesheet_configs.holiday_calendar_id -> holiday_calendars.id`` (ON DELETE RESTRICT).

Calendars are unique per (tenant, scope, country, state, client, legal entity, year) among non-archived rows
(expression index). Calendars / holidays are internal-only (org RLS without columns): portal reads go through
internal sessions after an ownership check; optional-holiday picks are visible to their Worker.

Revision ID: 20261005_213_01_holidays
Revises: 20261002_200_07
Create Date: 2026-10-05
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_213_01_holidays"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_leave",)
depends_on: str | Sequence[str] | None = "20261020_212_01_time_configs"

_ZERO = "'00000000-0000-0000-0000-000000000000'::uuid"


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


def upgrade() -> None:
    op.create_table(
        "holiday_calendars",
        *_base("holiday_calendars"),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("scope_kind", sa.String(20), nullable=False),
        sa.Column("country_code", sa.CHAR(2), server_default=sa.text("'IN'"), nullable=False),
        sa.Column("state_code", sa.CHAR(2), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=True),
        sa.Column("optional_allowance", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("copied_from_id", sa.Uuid(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.CheckConstraint(
            "scope_kind IN ('country', 'state', 'client', 'legal_entity')",
            name="ck_holiday_calendars_scope_kind",
        ),
        sa.CheckConstraint(
            "(scope_kind <> 'client' OR client_id IS NOT NULL) "
            "AND (scope_kind <> 'legal_entity' OR legal_entity_id IS NOT NULL) "
            "AND (scope_kind <> 'state' OR state_code IS NOT NULL)",
            name="ck_holiday_calendars_scope_ref",
        ),
        sa.CheckConstraint("year BETWEEN 2000 AND 2100", name="ck_holiday_calendars_year"),
        sa.CheckConstraint("optional_allowance >= 0", name="ck_holiday_calendars_optional_allowance"),
        sa.ForeignKeyConstraint(
            ["state_code"],
            ["gst_states.code"],
            name="fk_holiday_calendars_state_code_gst_states",
            ondelete="RESTRICT",
        ),
        _tfk("holiday_calendars", "client_id", "clients"),
        _tfk("holiday_calendars", "legal_entity_id", "legal_entities"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_holiday_calendars_tenant_id_id"),
    )
    op.create_index("ix_holiday_calendars_tenant_id", "holiday_calendars", ["tenant_id"])
    op.create_index("ix_holiday_calendars_year", "holiday_calendars", ["year", "scope_kind"])
    op.execute(
        "CREATE UNIQUE INDEX uq_holiday_calendars_scope ON holiday_calendars (tenant_id, scope_kind, "
        "coalesce(country_code, ''), coalesce(state_code, ''), "
        f"coalesce(client_id, {_ZERO}), coalesce(legal_entity_id, {_ZERO}), year) WHERE archived_at IS NULL"
    )

    op.create_table(
        "holidays",
        *_base("holidays"),
        sa.Column("calendar_id", sa.Uuid(), nullable=False),
        sa.Column("holiday_date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("type", sa.String(12), server_default=sa.text("'national'"), nullable=False),
        *_ts(),
        sa.CheckConstraint("type IN ('national', 'state', 'optional', 'client')", name="ck_holidays_type"),
        _tfk("holidays", "calendar_id", "holiday_calendars", "CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_holidays_tenant_id_id"),
        sa.UniqueConstraint("calendar_id", "holiday_date", "name", name="uq_holidays_calendar_date_name"),
    )
    op.create_index("ix_holidays_tenant_id", "holidays", ["tenant_id"])
    op.create_index("ix_holidays_calendar_id", "holidays", ["calendar_id", "holiday_date"])

    op.create_table(
        "worker_optional_holidays",
        *_base("worker_optional_holidays"),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("holiday_id", sa.Uuid(), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _tfk("worker_optional_holidays", "candidate_id", "candidates"),
        _tfk("worker_optional_holidays", "placement_id", "placements", "CASCADE"),
        _tfk("worker_optional_holidays", "holiday_id", "holidays", "CASCADE"),
        sa.UniqueConstraint("placement_id", "holiday_id", name="uq_worker_optional_holidays_pick"),
    )
    op.create_index("ix_worker_optional_holidays_tenant_id", "worker_optional_holidays", ["tenant_id"])
    op.create_index("ix_worker_optional_holidays_candidate_id", "worker_optional_holidays", ["candidate_id"])

    for table in ("holiday_calendars", "holidays", "worker_optional_holidays"):
        enable_rls(table)
    enable_org_rls("holiday_calendars")
    enable_org_rls("holidays")
    enable_org_rls("worker_optional_holidays", worker_col="candidate_id")

    op.create_foreign_key(
        "fk_timesheet_configs_holiday_calendar_id",
        "timesheet_configs",
        "holiday_calendars",
        ["holiday_calendar_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_timesheet_configs_holiday_calendar_id", "timesheet_configs", type_="foreignkey")
    for table in ("worker_optional_holidays", "holidays", "holiday_calendars"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
