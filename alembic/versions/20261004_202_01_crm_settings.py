# ruff: noqa: E501
"""Epic 15 (Sales CRM) 202_01: CRM settings, reason codes and lead sources (story 2.1).

Settings rows are created lazily; reason codes / sources are seeded lazily per tenant by the service. Tenant
RLS + internal-only org RLS (portal sessions see nothing).

Revision ID: 20261004_202_01_crm_settings
Revises: 20261002_200_07
Create Date: 2026-10-04
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261004_202_01_crm_settings"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_crm",)
depends_on: str | Sequence[str] | None = None


def base_columns(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"),
    ]


def upgrade() -> None:
    op.create_table(
        "crm_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stage_probabilities",
            sa.dialects.postgresql.JSONB(),
            server_default='{"qualification":10,"proposal":40,"negotiation":70,"won":100,"lost":0}',
            nullable=False,
        ),
        sa.Column("default_currency", sa.CHAR(3), server_default=sa.text("'INR'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_crm_settings"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_crm_settings_tenant_id_tenants", ondelete="RESTRICT"),
    )
    op.create_table(
        "crm_reason_codes",
        *base_columns("crm_reason_codes"),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('lead_disqualify', 'opportunity_lost', 'reopen')", name="ck_crm_reason_codes_kind"),
        sa.UniqueConstraint("tenant_id", "kind", "code", name="uq_crm_reason_codes_tenant_id_kind_code"),
    )
    op.create_index("ix_crm_reason_codes_tenant_id", "crm_reason_codes", ["tenant_id"])
    op.create_table(
        "crm_lead_sources",
        *base_columns("crm_lead_sources"),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "code", name="uq_crm_lead_sources_tenant_id_code"),
    )
    op.create_index("ix_crm_lead_sources_tenant_id", "crm_lead_sources", ["tenant_id"])
    for table in ("crm_settings", "crm_reason_codes", "crm_lead_sources"):
        enable_rls(table)
        enable_org_rls(table)  # internal-only


def downgrade() -> None:
    for table in ("crm_lead_sources", "crm_reason_codes", "crm_settings"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
