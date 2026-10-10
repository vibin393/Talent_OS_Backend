"""Epic 39 stories 39.5-39.6 (FR-199, FR-200): ``sequences``, ``sequence_steps``, ``enrollments``,
``marketing_trigger_caps``; FKs ``campaign_messages.enrollment_id`` / ``step_id``.

Revision ID: 20261007_303_03_mkt_sequences
Revises: 20261007_303_02_mkt_campaigns
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_303_03_mkt_sequences"
down_revision: str | tuple[str, ...] | None = "20261007_303_02_mkt_campaigns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("sequences", "sequence_steps", "enrollments", "marketing_trigger_caps")


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
    enable_org_rls(table)


def upgrade() -> None:
    t = "sequences"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_tenant(t),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("current_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("basis", sa.String(20), server_default=sa.text("'marketing'"), nullable=False),
        sa.Column(
            "exit_on",
            postgresql.JSONB(),
            server_default=sa.text(
                """'{"replied": true, "applied": true, "hired": true, "unsubscribed": true}'::jsonb"""
            ),
            nullable=False,
        ),
        sa.Column("re_enrol_gap_days", sa.SmallInteger(), server_default=sa.text("30"), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name="pk_sequences"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_sequences_tenant_id_id"),
        _fk("fk_sequences_owner_id_users", "owner_id", "users"),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'archived')", name=op.f("ck_sequences_status")
        ),
        sa.CheckConstraint("basis IN ('marketing', 'own_application')", name=op.f("ck_sequences_basis")),
        sa.CheckConstraint("re_enrol_gap_days BETWEEN 0 AND 365", name=op.f("ck_sequences_gap")),
        sa.CheckConstraint("current_version >= 0", name=op.f("ck_sequences_version")),
    )
    op.create_index("ix_sequences_owner_id", t, ["owner_id"])
    _finish(t)

    t = "sequence_steps"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_tenant(t),
        sa.Column("sequence_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("config", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name="pk_sequence_steps"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_sequence_steps_tenant_id_id"),
        sa.UniqueConstraint("sequence_id", "version", "position", name="uq_sequence_steps_version_position"),
        _fk("fk_sequence_steps_sequence_id", "sequence_id", "sequences", "CASCADE"),
        sa.CheckConstraint(
            "kind IN ('send', 'wait', 'branch', 'task', 'add_to_pool')", name=op.f("ck_sequence_steps_kind")
        ),
        sa.CheckConstraint("position >= 1 AND position <= 20", name=op.f("ck_sequence_steps_position")),
    )
    _finish(t)

    t = "enrollments"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_tenant(t),
        sa.Column("sequence_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'active'"), nullable=False),
        sa.Column("exit_reason", sa.String(14), nullable=True),
        sa.Column("position", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("origin", sa.String(10), server_default=sa.text("'user'"), nullable=False),
        sa.Column("workflow_rule_id", sa.Uuid(), nullable=True),
        sa.Column("snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("enrolled_by", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name="pk_enrollments"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_enrollments_tenant_id_id"),
        _fk("fk_enrollments_sequence_id", "sequence_id", "sequences", "CASCADE"),
        _fk("fk_enrollments_candidate_id", "candidate_id", "candidates", "CASCADE"),
        _fk("fk_enrollments_application_id", "application_id", "applications", "SET NULL"),
        _fk("fk_enrollments_snapshot_id", "snapshot_id", "audience_snapshots", "SET NULL"),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'completed', 'exited', 'suppressed')",
            name=op.f("ck_enrollments_status"),
        ),
        sa.CheckConstraint(
            "exit_reason IS NULL OR exit_reason IN ('replied', 'applied', 'hired', 'unsubscribed', 'manual')",
            name=op.f("ck_enrollments_exit_reason"),
        ),
        sa.CheckConstraint("origin IN ('user', 'workflow')", name=op.f("ck_enrollments_origin")),
    )
    op.create_index(
        "uq_enrollments_active",
        t,
        ["sequence_id", "candidate_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'paused')"),
    )
    op.create_index("ix_enrollments_due", t, ["status", "next_run_at"])
    op.create_index("ix_enrollments_candidate_id", t, ["candidate_id"])
    _finish(t)

    t = "marketing_trigger_caps"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "rule_id", "day", name="pk_marketing_trigger_caps"),
        sa.CheckConstraint("count >= 0", name=op.f("ck_marketing_trigger_caps_count")),
    )
    _finish(t)

    op.create_foreign_key(
        "fk_campaign_messages_enrollment_id",
        "campaign_messages",
        "enrollments",
        ["tenant_id", "enrollment_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_campaign_messages_step_id",
        "campaign_messages",
        "sequence_steps",
        ["tenant_id", "step_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_campaign_messages_step_id", "campaign_messages", type_="foreignkey")
    op.drop_constraint("fk_campaign_messages_enrollment_id", "campaign_messages", type_="foreignkey")
    for t in reversed(TABLES):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
