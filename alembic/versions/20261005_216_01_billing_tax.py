# ruff: noqa: E501
"""Epic 29 (Billing & GST) 216_01: ``billing_settings``, ``billing_client_settings`` (reverse charge /
annexure per Client), ``sac_codes`` and effective-dated ``tax_rates`` (non-overlapping per SAC, btree_gist).
SAC / rate seeds are written lazily per tenant by ``billing.rates.ensure_tax_master`` (FR-146).

E-invoice applicability and the IRP 30-day limit use the foundation columns ``gst_registrations.einvoice_applicable``
/ ``irp_reporting_limit``.

Revision ID: 20261005_216_01_billing_tax
Revises: 20261002_200_07
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_216_01_billing_tax"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_billing",)
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist SCHEMA public")
    op.create_table(
        "billing_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("round_off_mode", sa.String(length=15), server_default=sa.text("'none'"), nullable=False),
        sa.Column("cn_gst_deadline", sa.CHAR(length=5), server_default=sa.text("'11-30'"), nullable=False),
        sa.Column("allow_backdated_issue", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("stale_unbilled_days", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("pending_irn_alert_hours", sa.Integer(), server_default=sa.text("24"), nullable=False),
        sa.Column("annexure_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "cn_gst_deadline ~ '^[0-1][0-9]-[0-3][0-9]$'", name=op.f("ck_billing_settings_cn_gst_deadline")
        ),
        sa.CheckConstraint(
            "round_off_mode IN ('none', 'nearest_rupee')", name=op.f("ck_billing_settings_round_off_mode")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_billing_settings_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_billing_settings")),
    )
    op.create_table(
        "billing_client_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("reverse_charge", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("annexure", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_billing_client_settings_client_id_clients",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_billing_client_settings_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("client_id", name=op.f("pk_billing_client_settings")),
    )
    op.create_table(
        "sac_codes",
        sa.Column("code", sa.String(length=8), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column(
            "default_for_line_kind",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_sac_codes_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sac_codes")),
        sa.UniqueConstraint("tenant_id", "code", name=op.f("uq_sac_codes_tenant_id_code")),
    )
    op.create_index(op.f("ix_sac_codes_tenant_id"), "sac_codes", ["tenant_id"], unique=False)
    op.create_table(
        "tax_rates",
        sa.Column("sac_code", sa.String(length=8), nullable=False),
        sa.Column("rate_pct", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name=op.f("ck_tax_rates_dates")
        ),
        sa.CheckConstraint("rate_pct >= 0 AND rate_pct <= 100", name=op.f("ck_tax_rates_rate_pct")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_tax_rates_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tax_rates")),
    )
    op.create_index("ix_tax_rates_sac_code", "tax_rates", ["tenant_id", "sac_code"], unique=False)
    op.create_index(op.f("ix_tax_rates_tenant_id"), "tax_rates", ["tenant_id"], unique=False)
    op.execute(
        "ALTER TABLE tax_rates ADD CONSTRAINT ex_tax_rates_no_overlap EXCLUDE USING gist "
        "(tenant_id WITH =, sac_code WITH =, daterange(effective_from, effective_to, '[]') WITH &&)"
    )
    enable_rls("billing_settings")
    enable_rls("billing_client_settings")
    enable_rls("sac_codes")
    enable_rls("tax_rates")
    enable_org_rls("billing_settings")
    enable_org_rls("sac_codes")
    enable_org_rls("tax_rates")
    enable_org_rls("billing_client_settings")


def downgrade() -> None:
    disable_org_rls("tax_rates")
    disable_rls("tax_rates")
    op.drop_table("tax_rates")
    disable_org_rls("sac_codes")
    disable_rls("sac_codes")
    op.drop_table("sac_codes")
    disable_org_rls("billing_client_settings")
    disable_rls("billing_client_settings")
    op.drop_table("billing_client_settings")
    disable_org_rls("billing_settings")
    disable_rls("billing_settings")
    op.drop_table("billing_settings")
