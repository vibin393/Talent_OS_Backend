"""Recruiter assignments, SLA targets and SLA clocks (stories 4.5/4.6) (tenant-owned, RLS).

Revision ID: 20261001_44
Revises: 20261001_43
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20261001_44"
down_revision: str | tuple[str, ...] | None = "20261001_43"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = ("demand_assignments", "sla_targets", "demand_sla_clocks")


def upgrade() -> None:
    op.create_table(
        "demand_assignments",
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("assigned_by", sa.Uuid(), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_application_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_submission_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(user_id IS NULL) <> (team_id IS NULL)", name=op.f("ck_demand_assignments_one_assignee")
        ),
        sa.CheckConstraint(
            "NOT is_primary OR user_id IS NOT NULL", name=op.f("ck_demand_assignments_primary_is_user")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name="fk_demand_assignments_demand_id_demands",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "team_id"],
            ["teams.tenant_id", "teams.id"],
            name="fk_demand_assignments_team_id_teams",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_demand_assignments_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_demand_assignments_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demand_assignments")),
    )
    op.create_index("ix_demand_assignments_demand_id", "demand_assignments", ["demand_id"], unique=False)
    op.create_index("ix_demand_assignments_team_id", "demand_assignments", ["team_id"], unique=False)
    op.create_index(
        op.f("ix_demand_assignments_tenant_id"), "demand_assignments", ["tenant_id"], unique=False
    )
    op.create_index("ix_demand_assignments_user_id", "demand_assignments", ["user_id"], unique=False)
    op.create_index(
        "uq_demand_assignments_primary",
        "demand_assignments",
        ["demand_id"],
        unique=True,
        postgresql_where=sa.text("is_primary AND removed_at IS NULL"),
    )
    op.create_table(
        "sla_targets",
        sa.Column("milestone", sa.String(length=30), nullable=False),
        sa.Column("priority", sa.String(length=10), nullable=True),
        sa.Column("target_hours", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("clock", sa.String(length=10), server_default=sa.text("'business'"), nullable=False),
        sa.Column("at_risk_percent", sa.Integer(), server_default=sa.text("75"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("clock IN ('business', 'calendar')", name=op.f("ck_sla_targets_clock")),
        sa.CheckConstraint(
            "milestone IN ('assignment', 'first_submission', 'first_interview', 'client_feedback')",
            name=op.f("ck_sla_targets_milestone"),
        ),
        sa.CheckConstraint(
            "priority IS NULL OR priority IN ('low', 'medium', 'high', 'critical')",
            name=op.f("ck_sla_targets_priority"),
        ),
        sa.CheckConstraint("at_risk_percent BETWEEN 1 AND 99", name=op.f("ck_sla_targets_at_risk_percent")),
        sa.CheckConstraint("target_hours > 0", name=op.f("ck_sla_targets_target_hours")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_sla_targets_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sla_targets")),
        sa.UniqueConstraint(
            "tenant_id",
            "milestone",
            "priority",
            name=op.f("uq_sla_targets_tenant_id_milestone_priority"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(op.f("ix_sla_targets_tenant_id"), "sla_targets", ["tenant_id"], unique=False)
    op.create_table(
        "demand_sla_clocks",
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("milestone", sa.String(length=30), nullable=False),
        sa.Column("ref_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'healthy'"), nullable=False),
        sa.Column(
            "last_notified_status", sa.String(length=10), server_default=sa.text("'healthy'"), nullable=False
        ),
        sa.Column("target_hours", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("clock", sa.String(length=10), nullable=False),
        sa.Column("at_risk_percent", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("clock IN ('business', 'calendar')", name=op.f("ck_demand_sla_clocks_clock")),
        sa.CheckConstraint(
            "milestone IN ('assignment', 'first_submission', 'first_interview', 'client_feedback')",
            name=op.f("ck_demand_sla_clocks_milestone"),
        ),
        sa.CheckConstraint(
            "status IN ('healthy', 'at_risk', 'breached', 'met')", name=op.f("ck_demand_sla_clocks_status")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "demand_id"],
            ["demands.tenant_id", "demands.id"],
            name="fk_demand_sla_clocks_demand_id_demands",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_demand_sla_clocks_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demand_sla_clocks")),
    )
    op.create_index("ix_demand_sla_clocks_open", "demand_sla_clocks", ["completed_at"], unique=False)
    op.create_index(op.f("ix_demand_sla_clocks_tenant_id"), "demand_sla_clocks", ["tenant_id"], unique=False)
    op.create_index(
        "uq_demand_sla_clocks_milestone",
        "demand_sla_clocks",
        ["demand_id", "milestone", "ref_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
