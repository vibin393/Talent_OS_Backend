"""Epic 39 story 39.1 (FR-196): ``marketing_settings``, ``audiences``, ``audience_sources``,
``audience_list_entries``, ``audience_snapshots``, ``audience_members``.

First revision of branch ``p3_campaigns``. The only cross-branch ``depends_on`` of Phase 3 (build plan 1):
``audience_sources`` FK ``segments`` (Epic 38). Internal only (tenant RLS + org RLS without columns).

Revision ID: 20261007_303_01_mkt_audiences
Revises: 20261007_300_03_p3_alters
Depends on: 20261006_302_01_segments
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_303_01_mkt_audiences"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_campaigns",)
depends_on: str | Sequence[str] | None = ("20261006_302_01_segments",)

TABLES = (
    "marketing_settings",
    "audiences",
    "audience_sources",
    "audience_list_entries",
    "audience_snapshots",
    "audience_members",
)


def _ts() -> list[Any]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant(table: str) -> list[Any]:
    return [
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _fk(name: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _finish(table: str) -> None:
    op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
    enable_rls(table)
    enable_org_rls(table)  # no columns: internal sessions only


def upgrade() -> None:
    t = "marketing_settings"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("frequency_cap_count", sa.SmallInteger(), server_default=sa.text("3"), nullable=False),
        sa.Column("frequency_cap_days", sa.SmallInteger(), server_default=sa.text("7"), nullable=False),
        sa.Column("send_windows", postgresql.JSONB(), nullable=False),
        sa.Column("approval_threshold", sa.Integer(), server_default=sa.text("1000"), nullable=False),
        sa.Column("ab_window_hours", sa.SmallInteger(), server_default=sa.text("4"), nullable=False),
        sa.Column(
            "rate_per_minute", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("daily_cap", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("attribution_window_days", sa.SmallInteger(), server_default=sa.text("30"), nullable=False),
        sa.Column("default_timezone", sa.String(64), nullable=True),
        sa.Column("footer_contact", sa.String(500), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_reason", sa.String(20), nullable=True),
        sa.Column("paused_by", sa.Uuid(), nullable=True),
        sa.Column("last_dormant_scan_on", sa.Date(), nullable=True),
        *_ts(),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_marketing_settings"),
        sa.CheckConstraint(
            "frequency_cap_count BETWEEN 1 AND 3", name=op.f("ck_marketing_settings_cap_count")
        ),
        sa.CheckConstraint("frequency_cap_days = 7", name=op.f("ck_marketing_settings_cap_days")),
        sa.CheckConstraint("approval_threshold >= 1", name=op.f("ck_marketing_settings_threshold")),
        sa.CheckConstraint("ab_window_hours BETWEEN 1 AND 72", name=op.f("ck_marketing_settings_ab_window")),
        sa.CheckConstraint("attribution_window_days = 30", name=op.f("ck_marketing_settings_attribution")),
        sa.CheckConstraint(
            "paused_reason IS NULL OR paused_reason IN ('complaint_rate', 'bounce_rate', 'manual')",
            name=op.f("ck_marketing_settings_paused_reason"),
        ),
    )
    _finish(t)

    t = "audiences"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_tenant(t),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name="pk_audiences"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_audiences_tenant_id_id"),
        _fk("fk_audiences_owner_id_users", "owner_id", "users"),
        sa.CheckConstraint("status IN ('draft', 'active', 'archived')", name=op.f("ck_audiences_status")),
    )
    op.create_index("ix_audiences_owner_id", t, ["owner_id"])
    _finish(t)

    t = "audience_sources"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_tenant(t),
        sa.Column("audience_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("talent_pool_id", sa.Uuid(), nullable=True),
        sa.Column("segment_id", sa.Uuid(), nullable=True),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name="pk_audience_sources"),
        _fk("fk_audience_sources_audience_id_audiences", "audience_id", "audiences", "CASCADE"),
        _fk("fk_audience_sources_pool_id_talent_pools", "talent_pool_id", "talent_pools"),
        _fk("fk_audience_sources_segment_id_segments", "segment_id", "segments"),
        sa.CheckConstraint("mode IN ('include', 'exclude')", name=op.f("ck_audience_sources_mode")),
        sa.CheckConstraint("kind IN ('pool', 'segment')", name=op.f("ck_audience_sources_kind")),
        sa.CheckConstraint(
            "(kind = 'pool' AND talent_pool_id IS NOT NULL AND segment_id IS NULL)"
            " OR (kind = 'segment' AND segment_id IS NOT NULL AND talent_pool_id IS NULL)",
            name=op.f("ck_audience_sources_one_ref"),
        ),
    )
    op.create_index("ix_audience_sources_audience_id", t, ["audience_id"])
    _finish(t)

    t = "audience_list_entries"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("audience_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("audience_id", "mode", "candidate_id", name="pk_audience_list_entries"),
        _fk("fk_audience_list_entries_audience", "audience_id", "audiences", "CASCADE"),
        _fk("fk_audience_list_entries_candidate", "candidate_id", "candidates", "CASCADE"),
        sa.CheckConstraint("mode IN ('include', 'exclude')", name=op.f("ck_audience_list_entries_mode")),
    )
    op.create_index("ix_audience_list_entries_candidate_id", t, ["candidate_id"])
    _finish(t)

    t = "audience_snapshots"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_tenant(t),
        sa.Column("audience_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(12), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("total", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("counts", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audience_snapshots"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_audience_snapshots_tenant_id_id"),
        _fk("fk_audience_snapshots_audience_id", "audience_id", "audiences", "CASCADE"),
        sa.CheckConstraint(
            "purpose IN ('campaign', 'enrollment')", name=op.f("ck_audience_snapshots_purpose")
        ),
    )
    op.create_index("ix_audience_snapshots_audience_id", t, ["audience_id"])
    _finish(t)

    t = "audience_members"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_id", "candidate_id", name="pk_audience_members"),
        _fk("fk_audience_members_snapshot", "snapshot_id", "audience_snapshots", "CASCADE"),
        _fk("fk_audience_members_candidate", "candidate_id", "candidates", "CASCADE"),
    )
    op.create_index("ix_audience_members_candidate_id", t, ["candidate_id"])
    _finish(t)


def downgrade() -> None:
    for t in reversed(TABLES):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
