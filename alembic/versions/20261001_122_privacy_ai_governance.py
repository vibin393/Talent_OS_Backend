"""Epic 12 AI governance: ``ai_budget_alerts`` (one alert per tenant/month/threshold) and AI log indexes.

Revision ID: 20261001_122
Revises: 20261001_121
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_122"
down_revision: str | tuple[str, ...] | None = "20261001_121"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_budget_alerts",
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("threshold IN (80, 100, 120)", name=op.f("ck_ai_budget_alerts_threshold")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_ai_budget_alerts_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_budget_alerts")),
        sa.UniqueConstraint(
            "tenant_id", "period", "threshold", name=op.f("uq_ai_budget_alerts_tenant_id_period_threshold")
        ),
    )
    op.create_index(op.f("ix_ai_budget_alerts_tenant_id"), "ai_budget_alerts", ["tenant_id"], unique=False)
    enable_rls("ai_budget_alerts")
    op.create_index("ix_ai_actions_feature_created_at", "ai_actions", ["feature", "created_at"], unique=False)
    op.create_index("ix_ai_actions_initiator_id", "ai_actions", ["initiator_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ai_actions_initiator_id", table_name="ai_actions")
    op.drop_index("ix_ai_actions_feature_created_at", table_name="ai_actions")
    disable_rls("ai_budget_alerts")
    op.drop_table("ai_budget_alerts")
