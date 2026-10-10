# ruff: noqa: E501
"""Epic 68 (Webhooks, story 68.5, spec A2): the sandbox-only Echo receiver -- ``webhook_echo_endpoints`` and
``webhook_echo_receipts`` (7-day retention, ``webhooks.purge``). Tenant tables with RLS + org RLS.

Revision ID: 20261010_502_03_wh_echo
Revises: 20261010_502_02_wh_deliveries
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261010_502_03_wh_echo"
down_revision: str | tuple[str, ...] | None = "20261010_502_02_wh_deliveries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("webhook_echo_endpoints", "webhook_echo_receipts")


def _tenant(t: str) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT", name=f"fk_{t}_tenant_id_tenants"), nullable=False)


def upgrade() -> None:
    t = "webhook_echo_endpoints"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), primary_key=True),
        _tenant(t),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("label", sa.String(80), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("token_hash", name=f"uq_{t}_token_hash"),
    )
    t = "webhook_echo_receipts"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), primary_key=True),
        _tenant(t),
        sa.Column("echo_token_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("headers", postgresql.JSONB(), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
    )
    op.create_index("ix_webhook_echo_receipts_token_received", t, ["tenant_id", "echo_token_hash", sa.text("received_at DESC")])
    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
