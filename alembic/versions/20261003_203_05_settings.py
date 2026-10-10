# ruff: noqa: E501
"""Epic 16 203_05: commercials settings (per tenant) and contract renewal reminder dedupe.

Revision ID: 20261003_203_05_settings
Revises: 20261003_203_04_po_ledger
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261003_203_05_settings"
down_revision: str | tuple[str, ...] | None = "20261003_203_04_po_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "commercials_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("contract_renewal_lead_days", postgresql.ARRAY(sa.Integer()), server_default=sa.text("'{60,30}'"), nullable=False),
        sa.Column("po_alert_thresholds_pct", postgresql.ARRAY(sa.Integer()), server_default=sa.text("'{80,100}'"), nullable=False),
        sa.Column("po_expiry_alert_days", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("rate_exception_steps", postgresql.JSONB(), nullable=False),
        sa.Column("po_exception_steps", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_commercials_settings"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_commercials_settings_tenant_id_tenants", ondelete="RESTRICT"),
    )
    op.create_table(
        "contract_reminders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("lead_days", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_contract_reminders"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_contract_reminders_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "contract_id"], ["contracts.tenant_id", "contracts.id"], name="fk_contract_reminders_contract_id_contracts", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("contract_id", "end_date", "lead_days", name="uq_contract_reminders_contract_id_end_date_lead_days"),
    )
    op.create_index("ix_contract_reminders_tenant_id", "contract_reminders", ["tenant_id"])
    for table in ("commercials_settings", "contract_reminders"):
        enable_rls(table)
        enable_org_rls(table)  # internal-only


def downgrade() -> None:
    for table in ("contract_reminders", "commercials_settings"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
