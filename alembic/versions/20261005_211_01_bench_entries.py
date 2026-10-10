# ruff: noqa: E501
"""Epic 24 (Bench Management) 211_01: ``bench_settings`` and ``bench_entries`` (FR-125).

One open entry per Worker (partial UNIQUE on ``status IN ('on_bench','proposed')``); worker org RLS on
``candidate_id`` so a Worker portal session sees only its own entry (AD-26/27). FKs only to P1 ``candidates`` and
the contract-layer ``placements`` hub (200_07).

Revision ID: 20261005_211_01_bench_entries
Revises: 20261002_200_07
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_211_01_bench_entries"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_bench",)
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bench_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("hours_per_day", sa.Numeric(8, 2), server_default=sa.text("8.00"), nullable=False),
        sa.Column("monthly_rate_divisor", sa.Numeric(8, 2), server_default=sa.text("22.00"), nullable=False),
        sa.Column("bench_day_basis", sa.String(10), server_default=sa.text("'weekdays'"), nullable=False),
        sa.Column("upcoming_roll_off_days", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("notify_strong_bench_match", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "require_bench_review_for_vendor_release",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("contractor_opt_in_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_bench_settings"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_bench_settings_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("bench_day_basis IN ('calendar', 'weekdays')", name="ck_bench_settings_day_basis"),
        sa.CheckConstraint(
            "hours_per_day > 0 AND monthly_rate_divisor > 0", name="ck_bench_settings_positive"
        ),
    )
    # Tenant RLS only (no org policy): non-sensitive switches the Worker portal must read (contractor opt-in).
    enable_rls("bench_settings")

    op.create_table(
        "bench_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("source_placement_id", sa.Uuid(), nullable=True),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'on_bench'"), nullable=False),
        sa.Column("available_from", sa.Date(), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column(
            "skills_snapshot", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("location", postgresql.JSONB(), nullable=True),
        sa.Column("work_authorization", sa.Text(), nullable=True),
        sa.Column(
            "preferred_locations", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("cost_per_day", sa.Numeric(16, 2), nullable=True),
        sa.Column("cost_currency", sa.CHAR(3), nullable=True),
        sa.Column("cost_source", sa.String(20), nullable=True),
        sa.Column("cost_rate_unit_basis", sa.String(60), nullable=True),
        sa.Column(
            "proposed_application_ids",
            postgresql.ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("redeployed_placement_id", sa.Uuid(), nullable=True),
        sa.Column("released_reason", sa.String(30), nullable=True),
        sa.Column("released_note", sa.Text(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_bench_entries"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_bench_entries_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_bench_entries_candidate_id_candidates",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_placement_id"],
            ["placements.tenant_id", "placements.id"],
            name="fk_bench_entries_source_placement_id_placements",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "redeployed_placement_id"],
            ["placements.tenant_id", "placements.id"],
            name="fk_bench_entries_redeployed_placement_id_placements",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('on_bench', 'proposed', 'redeployed', 'released')", name="ck_bench_entries_status"
        ),
        sa.CheckConstraint(
            "origin IN ('placement_ended', 'contractor_opt_in', 'manual')", name="ck_bench_entries_origin"
        ),
        sa.CheckConstraint(
            "cost_source IS NULL OR cost_source IN ('rate_segment', 'manual')",
            name="ck_bench_entries_cost_source",
        ),
        sa.CheckConstraint(
            "released_reason IS NULL OR released_reason IN ('resigned', 'terminated', 'contractor_unavailable', 'other')",
            name="ck_bench_entries_released_reason",
        ),
        sa.CheckConstraint(
            "cost_per_day IS NULL OR cost_per_day >= 0", name="ck_bench_entries_cost_non_negative"
        ),
    )
    op.create_index("ix_bench_entries_tenant_id", "bench_entries", ["tenant_id"])
    op.create_index("ix_bench_entries_tenant_id_status", "bench_entries", ["tenant_id", "status"])
    op.create_index("ix_bench_entries_tenant_id_candidate_id", "bench_entries", ["tenant_id", "candidate_id"])
    op.create_index("ix_bench_entries_source_placement_id", "bench_entries", ["source_placement_id"])
    op.create_index(
        "uq_bench_entries_open",
        "bench_entries",
        ["tenant_id", "candidate_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('on_bench', 'proposed')"),
    )
    enable_rls("bench_entries")
    enable_org_rls("bench_entries", worker_col="candidate_id")


def downgrade() -> None:
    disable_org_rls("bench_entries")
    disable_rls("bench_entries")
    op.drop_table("bench_entries")
    disable_rls("bench_settings")
    op.drop_table("bench_settings")
