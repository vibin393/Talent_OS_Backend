"""Epic 17 (Vendors & Vendor Portal) 204_01: vendor record columns, GSTINs, bank accounts, document
requirements / portal uploads, settings; ``applications.source`` gains ``vendor``.

ALTER of the foundation hub ``vendors`` (PAN encrypted, address, contact, services, tier, owner, reasons).
Org RLS ``vendor_col="vendor_id"`` on the vendor-bearing tables; requirements and settings are internal-only.

Revision ID: 20261004_204_01_vendors_core
Revises: 20261002_200_07
Create Date: 2026-10-04
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261004_204_01_vendors_core"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_vendors",)
depends_on: str | Sequence[str] | None = None

P1_SOURCES = (
    "'manual', 'search', 'match', 'ai_match', 'saved_list', 'candidate_360', 'career_portal', 'import', 'api'"
)


def base_columns(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def vfk(table: str, ondelete: str = "CASCADE") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "vendor_id"],
        ["vendors.tenant_id", "vendors.id"],
        name=f"fk_{table}_vendor_id_vendors",
        ondelete=ondelete,
    )


def upgrade() -> None:
    # ---------------------------------------------------------------- vendors (ALTER hub)
    op.add_column("vendors", sa.Column("pan", sa.Text(), nullable=True))
    op.add_column("vendors", sa.Column("pan_last4", sa.String(4), nullable=True))
    op.add_column("vendors", sa.Column("pan_bidx", sa.String(32), nullable=True))
    op.add_column(
        "vendors",
        sa.Column("address", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.add_column(
        "vendors",
        sa.Column(
            "primary_contact", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
    )
    op.add_column(
        "vendors",
        sa.Column("services", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "vendors",
        sa.Column("skill_ids", postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "vendors",
        sa.Column("regions", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column("vendors", sa.Column("tier", sa.String(10), nullable=True))
    op.add_column("vendors", sa.Column("owner_id", sa.Uuid(), nullable=True))
    op.add_column("vendors", sa.Column("suspended_reason", sa.Text(), nullable=True))
    op.add_column("vendors", sa.Column("inactive_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_vendors_tier"), "vendors", "tier IS NULL OR tier IN ('gold', 'silver', 'bronze', 'custom')"
    )
    op.create_foreign_key(
        "fk_vendors_owner_id_users",
        "vendors",
        "users",
        ["tenant_id", "owner_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_vendors_pan_bidx", "vendors", ["tenant_id", "pan_bidx"])

    # ---------------------------------------------------------------- GSTINs
    op.create_table(
        "vendor_gstins",
        *base_columns("vendor_gstins"),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("gstin", sa.CHAR(15), nullable=False),
        sa.Column("state_code", sa.CHAR(2), nullable=False),
        vfk("vendor_gstins"),
        sa.ForeignKeyConstraint(
            ["state_code"], ["gst_states.code"], name="fk_vendor_gstins_state_code_gst_states"
        ),
        sa.UniqueConstraint("vendor_id", "gstin", name="uq_vendor_gstins_vendor_id_gstin"),
    )
    op.create_index("ix_vendor_gstins_tenant_id", "vendor_gstins", ["tenant_id"])
    op.create_index("ix_vendor_gstins_vendor_id", "vendor_gstins", ["vendor_id"])

    # ---------------------------------------------------------------- bank accounts
    op.create_table(
        "vendor_bank_accounts",
        *base_columns("vendor_bank_accounts"),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("account_name", sa.String(200), nullable=False),
        sa.Column("account_no", sa.Text(), nullable=False),
        sa.Column("account_no_last4", sa.String(4), nullable=False),
        sa.Column("account_no_bidx", sa.String(32), nullable=False),
        sa.Column("ifsc", sa.String(11), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'rejected', 'inactive')",
            name=op.f("ck_vendor_bank_accounts_status"),
        ),
        vfk("vendor_bank_accounts"),
    )
    op.create_index("ix_vendor_bank_accounts_tenant_id", "vendor_bank_accounts", ["tenant_id"])
    op.create_index("ix_vendor_bank_accounts_vendor_id", "vendor_bank_accounts", ["vendor_id"])

    # ---------------------------------------------------------------- document requirements + portal uploads
    op.create_table(
        "vendor_document_requirements",
        *base_columns("vendor_document_requirements"),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("is_mandatory", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint(
            "category IN ('agreement', 'insurance', 'registration', 'other')",
            name=op.f("ck_vendor_document_requirements_category"),
        ),
        sa.UniqueConstraint(
            "tenant_id", "category", name="uq_vendor_document_requirements_tenant_id_category"
        ),
    )
    op.create_index(
        "ix_vendor_document_requirements_tenant_id", "vendor_document_requirements", ["tenant_id"]
    )

    op.create_table(
        "vendor_document_uploads",
        *base_columns("vendor_document_uploads"),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("status", sa.String(15), server_default=sa.text("'pending_review'"), nullable=False),
        sa.Column("uploaded_by_vendor_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending_review', 'reviewed')", name=op.f("ck_vendor_document_uploads_status")
        ),
        vfk("vendor_document_uploads"),
    )
    op.create_index("ix_vendor_document_uploads_tenant_id", "vendor_document_uploads", ["tenant_id"])
    op.create_index("ix_vendor_document_uploads_vendor_id", "vendor_document_uploads", ["vendor_id"])

    # ---------------------------------------------------------------- settings (one row per tenant)
    op.create_table(
        "vendor_settings",
        *base_columns("vendor_settings"),
        sa.Column("ownership_window_days", sa.Integer(), server_default=sa.text("90"), nullable=False),
        sa.Column("duplicate_activity_months", sa.Integer(), server_default=sa.text("6"), nullable=False),
        sa.Column("suspension_grace_days", sa.Integer(), server_default=sa.text("7"), nullable=False),
        sa.Column(
            "mandatory_document_kinds",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{agreement,registration}'"),
            nullable=False,
        ),
        sa.Column(
            "scorecard_visible_to_vendor", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("vendor_labels", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("notice_link_days", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column(
            "require_bench_review_for_vendor_release",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ownership_window_days BETWEEN 1 AND 730", name=op.f("ck_vendor_settings_ownership_window")
        ),
        sa.CheckConstraint(
            "duplicate_activity_months BETWEEN 0 AND 60", name=op.f("ck_vendor_settings_duplicate_months")
        ),
        sa.CheckConstraint("suspension_grace_days BETWEEN 0 AND 365", name=op.f("ck_vendor_settings_grace")),
        sa.CheckConstraint("notice_link_days BETWEEN 1 AND 365", name=op.f("ck_vendor_settings_notice_days")),
        sa.UniqueConstraint("tenant_id", name="uq_vendor_settings_tenant_id"),
    )

    for table in (
        "vendor_gstins",
        "vendor_bank_accounts",
        "vendor_document_requirements",
        "vendor_document_uploads",
        "vendor_settings",
    ):
        enable_rls(table)
    for table in ("vendor_gstins", "vendor_bank_accounts", "vendor_document_uploads"):
        enable_org_rls(table, vendor_col="vendor_id")
    enable_org_rls("vendor_document_requirements")  # internal-only
    enable_org_rls("vendor_settings")  # internal-only

    # ---------------------------------------------------------------- applications.source gains 'vendor'
    op.drop_constraint(op.f("ck_applications_source"), "applications", type_="check")
    op.create_check_constraint(
        op.f("ck_applications_source"), "applications", f"source IN ({P1_SOURCES}, 'vendor')"
    )


def downgrade() -> None:
    op.execute("UPDATE applications SET source = 'manual' WHERE source = 'vendor'")
    op.drop_constraint(op.f("ck_applications_source"), "applications", type_="check")
    op.create_check_constraint(op.f("ck_applications_source"), "applications", f"source IN ({P1_SOURCES})")
    for table in (
        "vendor_settings",
        "vendor_document_uploads",
        "vendor_document_requirements",
        "vendor_bank_accounts",
        "vendor_gstins",
    ):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
    op.drop_index("ix_vendors_pan_bidx", table_name="vendors")
    op.drop_constraint("fk_vendors_owner_id_users", "vendors", type_="foreignkey")
    op.drop_constraint(op.f("ck_vendors_tier"), "vendors", type_="check")
    for col in (
        "inactive_reason", "suspended_reason", "owner_id", "tier", "regions", "skill_ids", "services",
        "primary_contact", "address", "pan_bidx", "pan_last4", "pan",
    ):  # fmt: skip
        op.drop_column("vendors", col)
