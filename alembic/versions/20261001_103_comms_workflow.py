# ruff: noqa: E501
"""Workflow rules and executions (Epic 10, story 10.4) (tenant-owned, RLS).

Revision ID: 20261001_103
Revises: 20261001_102
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20261001_103"
down_revision: str | tuple[str, ...] | None = "20261001_102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = ("workflow_rules", "workflow_executions")
STATUSES = (
    "matched",
    "not_matched",
    "succeeded",
    "partially_failed",
    "failed",
    "skipped_rate_limit",
    "skipped_loop",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=op.f(f"fk_{table}_tenant_id_tenants"), ondelete="RESTRICT"
    )


def upgrade() -> None:
    op.create_table(
        "workflow_rules",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("trigger", sa.String(length=64), nullable=False),
        sa.Column(
            "conditions", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "actions", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["users.tenant_id", "users.id"],
            name="fk_workflow_rules_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "updated_by"],
            ["users.tenant_id", "users.id"],
            name="fk_workflow_rules_updated_by_users",
            ondelete="RESTRICT",
        ),
        _tenant_fk("workflow_rules"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_rules")),
    )
    op.create_index(op.f("ix_workflow_rules_tenant_id"), "workflow_rules", ["tenant_id"], unique=False)
    op.create_index(
        "ix_workflow_rules_trigger_active", "workflow_rules", ["tenant_id", "trigger", "is_active"], unique=False
    )

    op.create_table(
        "workflow_executions",
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=10), server_default=sa.text("'live'"), nullable=False),
        sa.Column("trigger", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("chain_depth", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "condition_trace",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "action_results",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(_in("mode", ("live", "dry_run")), name=op.f("ck_workflow_executions_mode")),
        sa.CheckConstraint(_in("status", STATUSES), name=op.f("ck_workflow_executions_status")),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["workflow_rules.id"],
            name=op.f("fk_workflow_executions_rule_id_workflow_rules"),
            ondelete="CASCADE",
        ),
        _tenant_fk("workflow_executions"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_executions")),
        sa.UniqueConstraint(
            "rule_id",
            "event_id",
            "entity_id",
            "mode",
            name=op.f("uq_workflow_executions_rule_id_event_id_entity_id_mode"),
        ),
    )
    op.create_index(op.f("ix_workflow_executions_tenant_id"), "workflow_executions", ["tenant_id"], unique=False)
    op.create_index(
        "ix_workflow_executions_rule_created",
        "workflow_executions",
        ["tenant_id", "rule_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_workflow_executions_rule_entity", "workflow_executions", ["rule_id", "entity_id", "created_at"], unique=False
    )

    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
