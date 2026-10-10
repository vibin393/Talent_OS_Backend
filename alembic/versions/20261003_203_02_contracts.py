# ruff: noqa: E501
"""Epic 16 203_02: contract business columns, contract documents and SOW milestones (FR-89, §5.3).

ALTERs the contract-layer hub table ``contracts`` (keys / org columns untouched) and adds
``contract_documents`` (org columns inherited from the contract) and ``contract_milestones``.

Revision ID: 20261003_203_02_contracts
Revises: 20261003_203_01_billing_profiles
Create Date: 2026-10-03
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import InheritRef, disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261003_203_02_contracts"
down_revision: str | tuple[str, ...] | None = "20261003_203_01_billing_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS: list[sa.Column] = [  # type: ignore[type-arg]
    sa.Column("title", sa.String(300), nullable=True),
    sa.Column("counterparty_kind", sa.String(10), server_default=sa.text("'client'"), nullable=False),
    sa.Column("amends_end_date", sa.Date(), nullable=True),
    sa.Column("value_cap", sa.Numeric(16, 2), nullable=True),
    sa.Column("payment_terms_id", sa.Uuid(), nullable=True),
    sa.Column("conversion_fee_kind", sa.String(12), server_default=sa.text("'none'"), nullable=False),
    sa.Column("conversion_fee_value", sa.Numeric(16, 4), nullable=True),
    sa.Column("conversion_fee_waive_after_months", sa.Integer(), nullable=True),
    sa.Column("placement_fee_pct", sa.Numeric(7, 4), nullable=True),
    sa.Column("reverse_charge", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    sa.Column("share_bgv_outcome", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    sa.Column("notice_period_days", sa.Integer(), nullable=True),
    sa.Column("renewal_lead_days", postgresql.ARRAY(sa.Integer()), nullable=True),
    sa.Column("owner_id", sa.Uuid(), nullable=True),
    sa.Column("approval_request_id", sa.Uuid(), nullable=True),
    sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("signed_file_id", sa.Uuid(), nullable=True),
    sa.Column("signature_request_id", sa.Uuid(), nullable=True),
    sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("terminated_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("termination_date", sa.Date(), nullable=True),
    sa.Column("termination_reason", sa.Text(), nullable=True),
    sa.Column("opportunity_id", sa.Uuid(), nullable=True),
    sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
]
CHECKS: dict[str, str] = {
    "ck_contracts_counterparty_kind": "counterparty_kind IN ('client', 'vendor')",
    "ck_contracts_counterparty_matches": "(counterparty_kind = 'vendor') = (vendor_id IS NOT NULL)",
    "ck_contracts_vendor_agreement": "(contract_type = 'vendor_agreement') = (vendor_id IS NOT NULL)",
    "ck_contracts_conversion_fee_kind": "conversion_fee_kind IN ('none', 'pct_of_ctc', 'fixed')",
    "ck_contracts_amends_end_date": "amends_end_date IS NULL OR contract_type = 'amendment'",
    "ck_contracts_value_cap": "value_cap IS NULL OR value_cap > 0",
}
FKS: tuple[tuple[str, str], ...] = (
    ("payment_terms_id", "payment_terms"),
    ("owner_id", "users"),
    ("signed_file_id", "stored_files"),
)


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
    for column in COLUMNS:
        op.add_column("contracts", column)
    for name, expr in CHECKS.items():
        op.create_check_constraint(name, "contracts", expr)
    for column, target in FKS:
        op.create_foreign_key(
            f"fk_contracts_{column}_{target}", "contracts", target, ["tenant_id", column], ["tenant_id", "id"], ondelete="RESTRICT"
        )

    op.create_table(
        "contract_documents",
        *base_columns("contract_documents"),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint("kind IN ('draft', 'signed', 'annex', 'other')", name="ck_contract_documents_kind"),
        tfk("contract_documents", "contract_id", "contracts", "CASCADE"),
        tfk("contract_documents", "file_id", "stored_files"),
    )
    op.create_index("ix_contract_documents_tenant_id", "contract_documents", ["tenant_id"])
    op.create_index("ix_contract_documents_contract_id", "contract_documents", ["contract_id"])

    op.create_table(
        "contract_milestones",
        *base_columns("contract_milestones"),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'planned'"), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint("status IN ('planned', 'accepted', 'cancelled')", name="ck_contract_milestones_status"),
        sa.CheckConstraint("amount > 0", name="ck_contract_milestones_amount"),
        tfk("contract_milestones", "contract_id", "contracts"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_contract_milestones_tenant_id_id"),
    )
    op.create_index("ix_contract_milestones_tenant_id", "contract_milestones", ["tenant_id"])
    op.create_index("ix_contract_milestones_contract_id", "contract_milestones", ["contract_id"])

    enable_rls("contract_documents")
    enable_rls("contract_milestones")
    enable_org_rls(
        "contract_documents",
        client_col="client_id",
        vendor_col="vendor_id",
        inherit_from=InheritRef("contracts", "contract_id", ("client_id", "vendor_id")),
    )
    enable_org_rls(
        "contract_milestones",
        client_col="client_id",
        inherit_from=InheritRef("contracts", "contract_id", ("client_id",)),
    )


def downgrade() -> None:
    for table in ("contract_milestones", "contract_documents"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
    for column, target in FKS:
        op.drop_constraint(f"fk_contracts_{column}_{target}", "contracts", type_="foreignkey")
    for name in CHECKS:
        op.drop_constraint(name, "contracts", type_="check")
    for column in reversed(COLUMNS):
        op.drop_column("contracts", column.name)
