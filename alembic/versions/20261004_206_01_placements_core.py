# ruff: noqa: E501
"""Epic 19 (Placements) 206_01: placement hub columns, rate-segment columns + active-only EXCLUDE, status history,
placement settings.

Only the columns this branch owns are added (the hub columns of the foundation, Epic 13 A10, are reused).
``worker_type`` becomes NULL-able (permanent placements only). ``rate_segments`` stays append-only: the immutability
trigger is re-created allowing ``effective_to``, ``status`` and ``approval_request_id``; the foundation EXCLUDE is
replaced by the ``WHERE status = 'active'`` variant (pending / rejected segments never block).

Revision ID: 20261004_206_01_placements_core
Revises: 20261002_200_07
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    InheritRef,
    disable_org_rls,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_rls,
)

revision: str = "20261004_206_01_placements_core"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_placements",)
depends_on: str | Sequence[str] | None = None

ORG = ("client_id", "vendor_id", "candidate_id")

PLACEMENT_COLUMNS: list[sa.Column] = [  # type: ignore[type-arg]
    sa.Column("engagement_kind", sa.String(20), nullable=True),
    sa.Column("po_id", sa.Uuid(), nullable=True),
    sa.Column("rate_card_line_id", sa.Uuid(), nullable=True),
    sa.Column("holiday_calendar_id", sa.Uuid(), nullable=True),
    sa.Column("approval_route_ref", sa.String(100), nullable=True),
    sa.Column("work_location_text", sa.String(300), nullable=True),
    sa.Column("role_title", sa.String(300), nullable=True),
    sa.Column("manager_user_id", sa.Uuid(), nullable=True),
    sa.Column("client_manager_contact_id", sa.Uuid(), nullable=True),
    sa.Column("joining_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("joining_confirmed_via", sa.String(20), nullable=True),
    sa.Column("drop_reason", sa.String(30), nullable=True),
    sa.Column("drop_note", sa.Text(), nullable=True),
    sa.Column("dropout_risk", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    sa.Column("dropout_risk_rules", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column("dropout_cleared_by", sa.Uuid(), nullable=True),
    sa.Column("dropout_cleared_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("counter_offer_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("counter_offer_note", sa.Text(), nullable=True),
    sa.Column("setup_items", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column("onboarding_pct", sa.Integer(), nullable=True),
    sa.Column("bgv_summary", postgresql.JSONB(), nullable=True),
    sa.Column("compliance_blocked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    sa.Column("non_compliant", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    sa.Column("gate_alert_sent_on", sa.Date(), nullable=True),
    sa.Column("lock_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
]

SEGMENT_COLUMNS: list[sa.Column] = [  # type: ignore[type-arg]
    sa.Column("burden_pct", sa.Numeric(7, 4), nullable=True),
    sa.Column("vendor_burden_pct", sa.Numeric(7, 4), nullable=True),
    sa.Column("margin_amount", sa.Numeric(16, 2), nullable=True),
    sa.Column("margin_pct", sa.Numeric(7, 4), nullable=True),
    sa.Column("rate_check", postgresql.JSONB(), nullable=True),
    sa.Column("status", sa.String(20), server_default=sa.text("'active'"), nullable=False),
    sa.Column("approval_request_id", sa.Uuid(), nullable=True),
    sa.Column("reason", sa.Text(), nullable=True),
]


def upgrade() -> None:
    # ---------------------------------------------------------------- placements
    for col in PLACEMENT_COLUMNS:
        op.add_column("placements", col)
    op.alter_column("placements", "worker_type", existing_type=sa.String(20), nullable=True)
    op.create_check_constraint(
        "ck_placements_engagement_kind", "placements", "engagement_kind IS NULL OR engagement_kind IN ('contract', 'c2h', 'permanent')"
    )
    op.create_check_constraint(
        "ck_placements_worker_type_required", "placements", "worker_type IS NOT NULL OR engagement_kind = 'permanent'"
    )
    op.create_check_constraint(
        "ck_placements_joining_via",
        "placements",
        "joining_confirmed_via IS NULL OR joining_confirmed_via IN ('portal', 'secure_link', 'user')",
    )
    op.create_check_constraint(
        "ck_placements_drop_reason",
        "placements",
        "drop_reason IS NULL OR drop_reason IN ('declined', 'no_show', 'bgv_failed', 'client_cancelled')",
    )
    op.create_check_constraint(
        "ck_placements_onboarding_pct", "placements", "onboarding_pct IS NULL OR onboarding_pct BETWEEN 0 AND 100"
    )
    op.create_foreign_key(
        "fk_placements_po_id_purchase_orders", "placements", "purchase_orders", ["tenant_id", "po_id"], ["tenant_id", "id"], ondelete="RESTRICT"
    )
    op.create_foreign_key(
        "fk_placements_manager_user_id_users", "placements", "users", ["tenant_id", "manager_user_id"], ["tenant_id", "id"], ondelete="RESTRICT"
    )
    op.create_foreign_key(
        "fk_placements_client_manager_contact_id_contacts",
        "placements",
        "contacts",
        ["tenant_id", "client_manager_contact_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_placements_po_id", "placements", ["po_id"])
    op.create_index("ix_placements_status_start", "placements", ["status", "planned_start_date"])

    # ---------------------------------------------------------------- rate segments
    for col in SEGMENT_COLUMNS:
        op.add_column("rate_segments", col)
    op.create_check_constraint(
        "ck_rate_segments_status", "rate_segments", "status IN ('pending_approval', 'active', 'rejected')"
    )
    op.execute("ALTER TABLE rate_segments DROP CONSTRAINT IF EXISTS ex_rate_segments_no_overlap")
    op.execute(
        "ALTER TABLE rate_segments ADD CONSTRAINT ex_rate_segments_no_overlap EXCLUDE USING gist "
        "(tenant_id WITH =, placement_id WITH =, daterange(effective_from, effective_to, '[]') WITH &&) "
        "WHERE (status = 'active')"
    )
    enable_immutability(
        "rate_segments", status_column=None, allowed_columns=("effective_to", "status", "approval_request_id")
    )

    # ---------------------------------------------------------------- status history
    op.create_table(
        "placement_status_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("from_status", sa.String(20), nullable=True),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("actor_kind", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(100), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_placement_status_history"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_placement_status_history_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "placement_id"],
            ["placements.tenant_id", "placements.id"],
            name="fk_placement_status_history_placement_id_placements",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_placement_status_history_tenant_id", "placement_status_history", ["tenant_id"])
    op.create_index("ix_placement_status_history_placement_id", "placement_status_history", ["placement_id", "at"])
    enable_rls("placement_status_history")
    enable_org_rls(
        "placement_status_history",
        client_col="client_id",
        vendor_col="vendor_id",
        worker_col="candidate_id",
        inherit_from=InheritRef("placements", "placement_id", ORG),
    )

    # ---------------------------------------------------------------- settings
    op.create_table(
        "placement_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("human_id_prefix", sa.String(10), server_default=sa.text("'PLC'"), nullable=False),
        sa.Column("touchpoint_offsets", postgresql.ARRAY(sa.Integer()), server_default=sa.text("'{14,7,3,1}'::int[]"), nullable=False),
        sa.Column("gate_alert_offset_days", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("ending_soon_days", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("worker_type_by_employment_type", postgresql.JSONB(), nullable=False),
        sa.Column("dropout_rules", postgresql.JSONB(), nullable=False),
        sa.Column("touchpoint_email_template", sa.Text(), nullable=True),
        sa.Column("min_margin_pct", sa.Numeric(7, 4), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_placement_settings"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_placement_settings_tenant_id_tenants", ondelete="RESTRICT"),
        sa.CheckConstraint("gate_alert_offset_days BETWEEN 0 AND 60", name="ck_placement_settings_gate_alert"),
        sa.CheckConstraint("ending_soon_days BETWEEN 1 AND 365", name="ck_placement_settings_ending_soon"),
    )
    enable_rls("placement_settings")
    enable_org_rls("placement_settings")


def downgrade() -> None:
    disable_org_rls("placement_settings")
    disable_rls("placement_settings")
    op.drop_table("placement_settings")
    disable_org_rls("placement_status_history")
    disable_rls("placement_status_history")
    op.drop_table("placement_status_history")

    op.execute("ALTER TABLE rate_segments DROP CONSTRAINT IF EXISTS ex_rate_segments_no_overlap")
    op.execute("DELETE FROM rate_segments WHERE status <> 'active'")
    op.execute(
        "ALTER TABLE rate_segments ADD CONSTRAINT ex_rate_segments_no_overlap "
        "EXCLUDE USING gist (placement_id WITH =, daterange(effective_from, effective_to, '[]') WITH &&)"
    )
    enable_immutability("rate_segments", status_column=None, allowed_columns=("effective_to",))
    op.drop_constraint("ck_rate_segments_status", "rate_segments", type_="check")
    for col in reversed(SEGMENT_COLUMNS):
        op.drop_column("rate_segments", col.name)

    op.drop_index("ix_placements_status_start", table_name="placements")
    op.drop_index("ix_placements_po_id", table_name="placements")
    for fk in (
        "fk_placements_client_manager_contact_id_contacts",
        "fk_placements_manager_user_id_users",
        "fk_placements_po_id_purchase_orders",
    ):
        op.drop_constraint(fk, "placements", type_="foreignkey")
    for ck in (
        "ck_placements_onboarding_pct",
        "ck_placements_drop_reason",
        "ck_placements_joining_via",
        "ck_placements_worker_type_required",
        "ck_placements_engagement_kind",
    ):
        op.drop_constraint(ck, "placements", type_="check")
    op.execute("UPDATE placements SET worker_type = 'employee' WHERE worker_type IS NULL")
    op.alter_column("placements", "worker_type", existing_type=sa.String(20), nullable=False)
    for col in reversed(PLACEMENT_COLUMNS):
        op.drop_column("placements", col.name)
