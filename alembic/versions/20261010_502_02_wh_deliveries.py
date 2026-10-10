# ruff: noqa: E501
"""Epic 68 (Webhooks, story 68.4, spec A2): the delivery log ``webhook_deliveries``, RANGE-partitioned monthly on
``created_at`` (PK ``(id, created_at)``); tenant + org RLS on the parent and on every partition
(``create_monthly_partitions``; ``partitions.ensure_monthly`` keeps creating them).

Revision ID: 20261010_502_02_wh_deliveries
Revises: 20261010_502_01_webhooks
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import create_monthly_partitions, enable_org_rls, enable_rls

revision: str = "20261010_502_02_wh_deliveries"
down_revision: str | tuple[str, ...] | None = "20261010_502_01_webhooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    t = "webhook_deliveries"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT", name=f"fk_{t}_tenant_id_tenants"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("event_seq", sa.BigInteger(), nullable=True),
        sa.Column("subject_type", sa.String(40), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(12), server_default=sa.text("'event'"), nullable=False),
        sa.Column("replay_of_id", sa.Uuid(), nullable=True),
        sa.Column("body", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("attempt_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("last_status_code", sa.SmallInteger(), nullable=True),
        sa.Column("last_error", sa.String(60), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", "created_at", name=f"pk_{t}"),
        sa.CheckConstraint("kind IN ('event', 'replay', 'test', 'verification')", name=op.f("ck_webhook_deliveries_kind")),
        sa.CheckConstraint(
            "status IN ('pending', 'in_flight', 'retrying', 'succeeded', 'failed', 'held', 'cancelled')",
            name=op.f("ck_webhook_deliveries_status"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('pending', 'retrying') OR next_attempt_at IS NOT NULL", name=op.f("ck_webhook_deliveries_retry")
        ),
        postgresql_partition_by="RANGE (created_at)",
    )
    op.create_index("ix_webhook_deliveries_tenant_id", t, ["tenant_id"])
    op.create_index("ix_webhook_deliveries_sub_created", t, ["tenant_id", "subscription_id", sa.text("created_at DESC")])
    op.create_index(
        "ix_webhook_deliveries_due", t, ["status", "next_attempt_at"],
        postgresql_where=sa.text("status IN ('pending', 'retrying')"),
    )  # fmt: skip
    op.create_index("ix_webhook_deliveries_tenant_id_event_id", t, ["tenant_id", "event_id"])
    op.create_index("ix_webhook_deliveries_candidate_id", t, ["candidate_id"])
    enable_rls(t)
    enable_org_rls(t)
    create_monthly_partitions(t, months_ahead=3)


def downgrade() -> None:
    op.drop_table("webhook_deliveries")  # drops every partition with it
