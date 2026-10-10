# ruff: noqa: E501
"""Epic 15 (Sales CRM) 202_03: opportunities + role lines (story 2.3).

Revision ID: 20261004_202_03_crm_opps
Revises: 20261004_202_02_crm_leads
Create Date: 2026-10-04
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261004_202_03_crm_opps"
down_revision: str | tuple[str, ...] | None = "20261004_202_02_crm_leads"
branch_labels: str | Sequence[str] | None = None
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


def tfk(table: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=f"fk_{table}_{column}_{target}", ondelete=ondelete
    )


def upgrade() -> None:
    op.create_table(
        "opportunities",
        *base_columns("opportunities"),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("primary_contact_id", sa.Uuid(), nullable=True),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("service", sa.String(24), nullable=False),
        sa.Column("estimated_revenue", sa.Numeric(16, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'INR'"), nullable=False),
        sa.Column("probability_pct", sa.Numeric(5, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("probability_overridden", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("expected_close_date", sa.Date(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("account_manager_id", sa.Uuid(), nullable=True),
        sa.Column("stage", sa.String(16), server_default=sa.text("'qualification'"), nullable=False),
        sa.Column("stage_entered_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("lost_reason_code", sa.String(40), nullable=True),
        sa.Column("lost_competitor", sa.String(200), nullable=True),
        sa.Column("lost_note", sa.Text(), nullable=True),
        sa.Column("won_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lost_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_opportunities_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_opportunities_tenant_id_human_id"),
        sa.CheckConstraint("stage IN ('qualification', 'proposal', 'negotiation', 'won', 'lost')", name="ck_opportunities_stage"),
        sa.CheckConstraint("service IN ('contract_staffing', 'permanent_placement', 'project', 'other')", name="ck_opportunities_service"),
        sa.CheckConstraint("probability_pct >= 0 AND probability_pct <= 100", name="ck_opportunities_probability"),
        sa.CheckConstraint("estimated_revenue >= 0", name="ck_opportunities_revenue"),
        sa.ForeignKeyConstraint(["tenant_id", "client_id"], ["clients.tenant_id", "clients.id"], name="fk_opportunities_client_id_clients", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "primary_contact_id"], ["contacts.tenant_id", "contacts.id"], name="fk_opportunities_primary_contact_id_contacts", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "lead_id"], ["leads.tenant_id", "leads.id"], name="fk_opportunities_lead_id_leads", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "owner_id"], ["users.tenant_id", "users.id"], name="fk_opportunities_owner_id_users", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "account_manager_id"], ["users.tenant_id", "users.id"], name="fk_opportunities_account_manager_id_users", ondelete="RESTRICT"),
    )
    op.create_index("ix_opportunities_tenant_id", "opportunities", ["tenant_id"])
    op.create_index("ix_opportunities_stage_close", "opportunities", ["tenant_id", "stage", "expected_close_date"])
    op.create_index("ix_opportunities_owner_stage", "opportunities", ["tenant_id", "owner_id", "stage"])
    op.create_index("ix_opportunities_client", "opportunities", ["tenant_id", "client_id"])

    op.create_table(
        "opportunity_lines",
        *base_columns("opportunity_lines"),
        sa.Column("opportunity_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("positions", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("expected_bill_rate", sa.Numeric(14, 4), nullable=True),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'INR'"), nullable=False),
        sa.Column("rate_unit", sa.String(10), nullable=True),
        sa.Column("location", sa.String(300), nullable=True),
        sa.Column("experience_min_years", sa.Numeric(4, 1), nullable=True),
        sa.Column("experience_max_years", sa.Numeric(4, 1), nullable=True),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_opportunity_lines_tenant_id_id"),
        sa.CheckConstraint("positions >= 1", name="ck_opportunity_lines_positions"),
        sa.CheckConstraint("rate_unit IS NULL OR rate_unit IN ('hour', 'day', 'month', 'year')", name="ck_opportunity_lines_rate_unit"),
        tfk("opportunity_lines", "opportunity_id", "opportunities", "CASCADE"),
    )
    op.create_index("ix_opportunity_lines_tenant_id", "opportunity_lines", ["tenant_id"])
    op.create_index("ix_opportunity_lines_opportunity", "opportunity_lines", ["opportunity_id", "position"])

    for table in ("opportunities", "opportunity_lines"):
        enable_rls(table)
        enable_org_rls(table)  # internal-only


def downgrade() -> None:
    for table in ("opportunity_lines", "opportunities"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
