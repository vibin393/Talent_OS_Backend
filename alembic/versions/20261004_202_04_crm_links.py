# ruff: noqa: E501
"""Epic 15 (Sales CRM) 202_04: records created by a Won conversion (story 2.4). ``record_id`` is polymorphic
(client / contract / rate_card / demand) and has no FK.

Revision ID: 20261004_202_04_crm_links
Revises: 20261004_202_03_crm_opps
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261004_202_04_crm_links"
down_revision: str | tuple[str, ...] | None = "20261004_202_03_crm_opps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crm_record_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("opportunity_id", sa.Uuid(), nullable=False),
        sa.Column("record_kind", sa.String(12), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_crm_record_links"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_crm_record_links_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"], ["opportunities.tenant_id", "opportunities.id"],
            name="fk_crm_record_links_opportunity_id_opportunities", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "opportunity_id", "record_kind", "record_id", name="uq_crm_record_links_record"),
        sa.CheckConstraint("record_kind IN ('client', 'contract', 'rate_card', 'demand')", name="ck_crm_record_links_record_kind"),
    )
    op.create_index("ix_crm_record_links_tenant_id", "crm_record_links", ["tenant_id"])
    op.create_index("ix_crm_record_links_record", "crm_record_links", ["tenant_id", "record_kind", "record_id"])
    enable_rls("crm_record_links")
    enable_org_rls("crm_record_links")  # internal-only


def downgrade() -> None:
    disable_org_rls("crm_record_links")
    disable_rls("crm_record_links")
    op.drop_table("crm_record_links")
