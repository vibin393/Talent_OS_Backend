# ruff: noqa: E501
"""Phase 2 Epic 33 (FR-174, AD-38): custom Career Portal domains.

``custom_domains`` is a platform table (no RLS, no PII; listed in ``app.core.tenancy.PLATFORM_TABLES``): one
live (non-removed) row per host and per tenant, verified by a DNS TXT record.

Revision ID: 20261003_220_03_custom_domains
Revises: 20261003_220_02_comms_channels
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261003_220_03_custom_domains"
down_revision: str | tuple[str, ...] | None = "20261003_220_02_comms_channels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "custom_domains",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("host", postgresql.CITEXT(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("txt_token", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'verified', 'failed', 'removed')", name=op.f("ck_custom_domains_status")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_custom_domains_tenant_id_tenants"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_custom_domains")),
    )
    op.create_index("uq_custom_domains_host_live", "custom_domains", ["host"], unique=True, postgresql_where=sa.text("status <> 'removed'"))
    op.create_index(
        "uq_custom_domains_tenant_live", "custom_domains", ["tenant_id"], unique=True, postgresql_where=sa.text("status <> 'removed'")
    )


def downgrade() -> None:
    op.drop_table("custom_domains")
