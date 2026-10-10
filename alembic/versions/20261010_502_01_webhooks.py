# ruff: noqa: E501
"""Epic 68 (Webhooks, story 68.1, spec A2): ``webhook_subscriptions`` and ``webhook_delivery_keys``. Tenant tables with
RLS + org RLS.

Revision ID: 20261010_502_01_webhooks
Revises: 20261009_500_06_p5_vault_drop
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261010_502_01_webhooks"
down_revision: str | tuple[str, ...] | None = "20261009_500_06_p5_vault_drop"
branch_labels: str | Sequence[str] | None = ("p5_webhooks",)
depends_on: str | Sequence[str] | None = None

TABLES = ("webhook_subscriptions", "webhook_delivery_keys")


def _ts(name: str, *, nullable: bool = True, now: bool = False) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text("now()") if now else None, nullable=nullable)


def upgrade() -> None:
    t = "webhook_subscriptions"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT", name=f"fk_{t}_tenant_id_tenants"), nullable=False),
        _ts("created_at", nullable=False, now=True),
        _ts("updated_at", nullable=False, now=True),
        sa.Column("owner_kind", sa.String(12), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_host", sa.String(253), nullable=False),
        sa.Column("description", sa.String(300), nullable=True),
        sa.Column("event_types", postgresql.ARRAY(sa.String(60)), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("all_events", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("filters", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.String(24), server_default=sa.text("'pending_verification'"), nullable=False),
        sa.Column("disabled_reason", sa.String(20), nullable=True),
        _ts("verified_at"),
        sa.Column("verification_error", sa.String(80), nullable=True),
        sa.Column("verification_attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_in_flight", sa.SmallInteger(), server_default=sa.text("5"), nullable=False),
        sa.Column("suppress_own_changes", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("failure_streak_started_at"),
        _ts("streak_notified_at"),
        _ts("last_success_at"),
        _ts("last_failure_at"),
        _ts("disabled_at"),
        _ts("secret_rotated_at"),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_installation_id", sa.Uuid(), nullable=True),
        sa.Column("notify_user_id", sa.Uuid(), nullable=True),
        _ts("deleted_at"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "installation_id"], ["app_installations.tenant_id", "app_installations.id"],
            name="fk_webhook_subscriptions_installation_id_app_installations", ondelete="RESTRICT",
        ),
        sa.CheckConstraint("owner_kind IN ('tenant', 'installation')", name=op.f("ck_webhook_subscriptions_owner_kind")),
        sa.CheckConstraint("status IN ('pending_verification', 'active', 'disabled')", name=op.f("ck_webhook_subscriptions_status")),
        sa.CheckConstraint(
            "disabled_reason IS NULL OR disabled_reason IN ('manual', 'gone', 'failing', 'egress_refused', 'uninstalled', 'scope_lost')",
            name=op.f("ck_webhook_subscriptions_disabled_reason"),
        ),
        sa.CheckConstraint("(owner_kind = 'installation') = (installation_id IS NOT NULL)", name=op.f("ck_webhook_subscriptions_owner")),
        sa.CheckConstraint("all_events OR cardinality(event_types) > 0", name=op.f("ck_webhook_subscriptions_types")),
        sa.CheckConstraint("(status = 'disabled') = (disabled_reason IS NOT NULL)", name=op.f("ck_webhook_subscriptions_disabled")),
        sa.CheckConstraint("max_in_flight BETWEEN 1 AND 5", name=op.f("ck_webhook_subscriptions_max_in_flight")),
    )
    op.create_index("ix_webhook_subscriptions_tenant_id_status", t, ["tenant_id", "status"])
    op.create_index("ix_webhook_subscriptions_tenant_id_installation_id", t, ["tenant_id", "installation_id"])
    op.create_index("ix_webhook_subscriptions_url_host", t, ["url_host"])

    t = "webhook_delivery_keys"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT", name=f"fk_{t}_tenant_id_tenants"), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        _ts("created_at", nullable=False, now=True),
        sa.PrimaryKeyConstraint("tenant_id", "subscription_id", "event_id", name=f"pk_{t}"),
    )
    op.create_index("ix_webhook_delivery_keys_created_at", t, ["created_at"])
    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
