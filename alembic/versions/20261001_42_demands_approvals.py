"""Generic approval chains, requests and decisions (story 4.2) (tenant-owned, RLS).

Revision ID: 20261001_42
Revises: 20261001_41
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20261001_42"
down_revision: str | tuple[str, ...] | None = "20261001_41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = (
    "approval_chains",
    "approval_chain_steps",
    "approval_requests",
    "approval_decisions",
)


def upgrade() -> None:
    op.create_table(
        "approval_chains",
        sa.Column("subject_type", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "subject_type IN ('demand', 'offer', 'margin_override')",
            name=op.f("ck_approval_chains_subject_type"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_approval_chains_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_chains")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_approval_chains_tenant_id_id")),
    )
    op.create_index(op.f("ix_approval_chains_tenant_id"), "approval_chains", ["tenant_id"], unique=False)
    op.create_index(
        "uq_approval_chains_active_subject",
        "approval_chains",
        ["tenant_id", "subject_type"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "approval_chain_steps",
        sa.Column("chain_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("approver_user_id", sa.Uuid(), nullable=True),
        sa.Column("approver_role_id", sa.Uuid(), nullable=True),
        sa.Column("condition", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(approver_user_id IS NULL) <> (approver_role_id IS NULL)",
            name=op.f("ck_approval_chain_steps_one_approver"),
        ),
        sa.CheckConstraint("position >= 1", name=op.f("ck_approval_chain_steps_position")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "approver_role_id"],
            ["roles.tenant_id", "roles.id"],
            name="fk_approval_chain_steps_approver_role_id_roles",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "approver_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_approval_chain_steps_approver_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "chain_id"],
            ["approval_chains.tenant_id", "approval_chains.id"],
            name="fk_approval_chain_steps_chain_id_approval_chains",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_approval_chain_steps_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_chain_steps")),
        sa.UniqueConstraint("chain_id", "position", name=op.f("uq_approval_chain_steps_chain_id_position")),
    )
    op.create_index(
        op.f("ix_approval_chain_steps_tenant_id"), "approval_chain_steps", ["tenant_id"], unique=False
    )
    op.create_table(
        "approval_requests",
        sa.Column("subject_type", sa.String(length=30), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("chain_id", sa.Uuid(), nullable=True),
        sa.Column("chain_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("current_position", sa.Integer(), nullable=True),
        sa.Column("current_approver_user_id", sa.Uuid(), nullable=True),
        sa.Column("current_approver_role_id", sa.Uuid(), nullable=True),
        sa.Column("submitted_by", sa.Uuid(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'changes_requested', 'cancelled')",
            name=op.f("ck_approval_requests_status"),
        ),
        sa.CheckConstraint(
            "subject_type IN ('demand', 'offer', 'margin_override')",
            name=op.f("ck_approval_requests_subject_type"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_approval_requests_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_requests")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_approval_requests_tenant_id_id")),
    )
    op.create_index(
        "ix_approval_requests_pending_role",
        "approval_requests",
        ["current_approver_role_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_approval_requests_pending_user",
        "approval_requests",
        ["current_approver_user_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_approval_requests_subject", "approval_requests", ["subject_type", "subject_id"], unique=False
    )
    op.create_index(op.f("ix_approval_requests_tenant_id"), "approval_requests", ["tenant_id"], unique=False)
    op.create_table(
        "approval_decisions",
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("step_name", sa.String(length=200), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("evaluated_condition", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('approved', 'rejected', 'changes_requested', 'skipped')",
            name=op.f("ck_approval_decisions_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "request_id"],
            ["approval_requests.tenant_id", "approval_requests.id"],
            name="fk_approval_decisions_request_id_approval_requests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_approval_decisions_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_decisions")),
    )
    op.create_index("ix_approval_decisions_request_id", "approval_decisions", ["request_id"], unique=False)
    op.create_index(
        op.f("ix_approval_decisions_tenant_id"), "approval_decisions", ["tenant_id"], unique=False
    )
    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
