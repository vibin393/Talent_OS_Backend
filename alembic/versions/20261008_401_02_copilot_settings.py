# ruff: noqa: E501
"""Epic 54 story 54.5 (FR-279): ``copilot_settings`` -- one row per tenant (retention, per-user rate limit, open
conversation cap, feedback comments). Draft expiry lives in ``agent_configs.proposal_expiry_hours`` (foundation).

Tenant RLS + internal-only org RLS.

Revision ID: 20261008_401_02_copilot_settings
Revises: 20261008_401_01_copilot_convs
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_401_02_copilot_settings"
down_revision: str | tuple[str, ...] | None = "20261008_401_01_copilot_convs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    t = "copilot_settings"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("retention_days", sa.Integer(), server_default=sa.text("90"), nullable=False),
        sa.Column("questions_per_hour", sa.Integer(), server_default=sa.text("60"), nullable=False),
        sa.Column("max_open_conversations", sa.Integer(), server_default=sa.text("50"), nullable=False),
        sa.Column("allow_feedback_comments", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_copilot_settings"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_copilot_settings_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "updated_by"],
            ["users.tenant_id", "users.id"],
            name="fk_copilot_settings_updated_by_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "retention_days BETWEEN 7 AND 365", name=op.f("ck_copilot_settings_retention_days")
        ),
        sa.CheckConstraint(
            "questions_per_hour BETWEEN 5 AND 300", name=op.f("ck_copilot_settings_questions_per_hour")
        ),
        sa.CheckConstraint(
            "max_open_conversations BETWEEN 5 AND 200",
            name=op.f("ck_copilot_settings_max_open_conversations"),
        ),
    )
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    disable_org_rls("copilot_settings")
    disable_rls("copilot_settings")
    op.drop_table("copilot_settings")
