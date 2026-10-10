# ruff: noqa: E501
"""Epic 69 (Integration Marketplace, story 69.2, spec A2): tenant table ``installation_health_log`` (connection test
history; not audited; retention class ``connector_payloads``; no personal data). RLS + internal-only org RLS.

Revision ID: 20261010_503_02_mkt_tenant
Revises: 20261010_503_01_marketplace
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261010_503_02_mkt_tenant"
down_revision: str | tuple[str, ...] | None = "20261010_503_01_marketplace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "installation_health_log"


def upgrade() -> None:
    op.create_table(
        T,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger", sa.String(12), nullable=False),
        sa.Column("result", sa.String(10), nullable=False),
        sa.Column("error_code", sa.String(60), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{T}"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{T}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "installation_id"],
            ["app_installations.tenant_id", "app_installations.id"],
            name=f"fk_{T}_installation_id_inst",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{T}_tenant_id_id"),
        sa.CheckConstraint(
            "trigger IN ('install', 'manual', 'scheduled', 'job_failures')", name=op.f(f"ck_{T}_trigger")
        ),
        sa.CheckConstraint("result IN ('ok', 'degraded', 'failing')", name=op.f(f"ck_{T}_result")),
    )
    op.create_index(
        "ix_installation_health_log_inst_checked", T, ["installation_id", sa.text("checked_at DESC")]
    )
    op.create_index(f"ix_{T}_tenant_id", T, ["tenant_id"])
    enable_rls(T)
    enable_org_rls(T)


def downgrade() -> None:
    disable_org_rls(T)
    disable_rls(T)
    op.drop_table(T)
