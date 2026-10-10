# ruff: noqa: E501
"""Epic 16 (Client Commercials) 203_01: payment terms and billing profiles (FR-88).

``payment_terms`` (one default per tenant), ``billing_profiles`` (one per client), ``billing_addresses`` (one
default per profile), ``billing_profile_contacts``. Tenant RLS everywhere; org RLS on the client-bearing tables.

Revision ID: 20261003_203_01_billing_profiles
Revises: 20261002_200_07
Create Date: 2026-10-03
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261003_203_01_billing_profiles"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_commercials",)
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
        "payment_terms",
        *base_columns("payment_terms"),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("days >= 0 AND days <= 365", name="ck_payment_terms_days"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_payment_terms_tenant_id_id"),
    )
    op.create_index("ix_payment_terms_tenant_id", "payment_terms", ["tenant_id"])
    op.create_index(
        "uq_payment_terms_default", "payment_terms", ["tenant_id"], unique=True,
        postgresql_where=sa.text("is_default AND archived_at IS NULL"),
    )

    op.create_table(
        "billing_profiles",
        *base_columns("billing_profiles"),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("legal_name", sa.String(300), nullable=False),
        sa.Column("pan", sa.CHAR(10), nullable=True),
        sa.Column("tax_treatment", sa.String(20), server_default=sa.text("'registered'"), nullable=False),
        sa.Column("payment_terms_id", sa.Uuid(), nullable=True),
        sa.Column("po_required", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("delivery_preference", sa.String(20), server_default=sa.text("'email'"), nullable=False),
        sa.Column("tds_section_code", sa.String(20), nullable=True),
        sa.Column("tds_rate_pct", sa.Numeric(7, 4), nullable=True),
        sa.Column("gst_tds_applicable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("preferred_vendor_status", sa.String(20), server_default=sa.text("'none'"), nullable=False),
        sa.Column("msp_name", sa.String(200), nullable=True),
        sa.Column("vms_name", sa.String(200), nullable=True),
        sa.Column("vms_reference", sa.String(200), nullable=True),
        sa.CheckConstraint("tax_treatment IN ('registered', 'unregistered', 'sez', 'export')", name="ck_billing_profiles_tax_treatment"),
        sa.CheckConstraint("delivery_preference IN ('email', 'portal', 'email_and_portal')", name="ck_billing_profiles_delivery_preference"),
        sa.CheckConstraint("preferred_vendor_status IN ('none', 'empanelled', 'preferred')", name="ck_billing_profiles_preferred_vendor_status"),
        sa.CheckConstraint("tds_rate_pct IS NULL OR (tds_rate_pct >= 0 AND tds_rate_pct <= 100)", name="ck_billing_profiles_tds_rate"),
        tfk("billing_profiles", "client_id", "clients"),
        tfk("billing_profiles", "payment_terms_id", "payment_terms"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_billing_profiles_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "client_id", name="uq_billing_profiles_tenant_id_client_id"),
    )
    op.create_index("ix_billing_profiles_tenant_id", "billing_profiles", ["tenant_id"])

    op.create_table(
        "billing_addresses",
        *base_columns("billing_addresses"),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("billing_profile_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("line1", sa.String(300), nullable=False),
        sa.Column("line2", sa.String(300), nullable=True),
        sa.Column("city", sa.String(100), nullable=False),
        sa.Column("state_code", sa.CHAR(2), nullable=True),
        sa.Column("postal_code", sa.String(20), nullable=True),
        sa.Column("country", sa.CHAR(2), server_default=sa.text("'IN'"), nullable=False),
        sa.Column("gstin", sa.CHAR(15), nullable=True),
        sa.Column("is_sez", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        tfk("billing_addresses", "client_id", "clients"),
        tfk("billing_addresses", "billing_profile_id", "billing_profiles", "CASCADE"),
        sa.ForeignKeyConstraint(["state_code"], ["gst_states.code"], name="fk_billing_addresses_state_code_gst_states"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_billing_addresses_tenant_id_id"),
    )
    op.create_index("ix_billing_addresses_tenant_id", "billing_addresses", ["tenant_id"])
    op.create_index("ix_billing_addresses_client_id", "billing_addresses", ["client_id"])
    op.create_index(
        "uq_billing_addresses_default", "billing_addresses", ["billing_profile_id"], unique=True,
        postgresql_where=sa.text("is_default AND archived_at IS NULL"),
    )

    op.create_table(
        "billing_profile_contacts",
        *base_columns("billing_profile_contacts"),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("billing_profile_id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(4), nullable=False),
        sa.CheckConstraint("role IN ('to', 'cc')", name="ck_billing_profile_contacts_role"),
        tfk("billing_profile_contacts", "client_id", "clients"),
        tfk("billing_profile_contacts", "billing_profile_id", "billing_profiles", "CASCADE"),
        tfk("billing_profile_contacts", "contact_id", "contacts", "CASCADE"),
        sa.UniqueConstraint("billing_profile_id", "contact_id", name="uq_billing_profile_contacts_billing_profile_id_contact_id"),
    )
    op.create_index("ix_billing_profile_contacts_tenant_id", "billing_profile_contacts", ["tenant_id"])

    for table in ("payment_terms", "billing_profiles", "billing_addresses", "billing_profile_contacts"):
        enable_rls(table)
    enable_org_rls("payment_terms")  # internal-only
    for table in ("billing_profiles", "billing_addresses", "billing_profile_contacts"):
        enable_org_rls(table, client_col="client_id")


def downgrade() -> None:
    for table in ("billing_profile_contacts", "billing_addresses", "billing_profiles", "payment_terms"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
