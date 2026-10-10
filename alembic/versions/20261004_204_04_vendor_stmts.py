# ruff: noqa: E501
"""Epic 17 204_04: Vendor Payable Statements (FR-98, AD-23): ``vendor_payable_statements`` and
``vendor_statement_lines``. Org RLS ``vendor_col``; immutable once finalized (lines follow the parent's state).

Revision ID: 20261004_204_04_vendor_stmts
Revises: 20261004_204_03_vendor_subs
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import (
    ParentRef,
    disable_immutability,
    disable_org_rls,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_rls,
)

revision: str = "20261004_204_04_vendor_stmts"
down_revision: str | tuple[str, ...] | None = "20261004_204_03_vendor_subs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    s = "vendor_payable_statements"
    op.create_table(
        s,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("pay_period_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(12), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'INR'"), nullable=False),
        sa.Column("total_amount", sa.Numeric(16, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("built_by", sa.Uuid(), nullable=True),
        sa.Column("finalized_by", sa.Uuid(), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("csv_file_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_vendor_payable_statements"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_vendor_payable_statements_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "vendor_id"],
            ["vendors.tenant_id", "vendors.id"],
            name="fk_vendor_payable_statements_vendor_id_vendors",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_vendor_payable_statements_legal_entity_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'finalized', 'superseded')", name=op.f("ck_vendor_payable_statements_status")
        ),
        sa.CheckConstraint("period_end >= period_start", name=op.f("ck_vendor_payable_statements_period")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_vendor_payable_statements_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_vendor_payable_statements_tenant_id_human_id"),
    )
    op.create_index("ix_vendor_payable_statements_tenant_id", s, ["tenant_id"])
    op.create_index("ix_vendor_payable_statements_vendor_id", s, ["vendor_id"])

    ln = "vendor_statement_lines"
    op.create_table(
        ln,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("statement_id", sa.Uuid(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("line_kind", sa.String(12), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=True),
        sa.Column("worker_name_snapshot", sa.String(200), nullable=True),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("time_category", sa.String(40), nullable=True),
        sa.Column("quantity", sa.Numeric(8, 2), nullable=False),
        sa.Column("vendor_rate", sa.Numeric(14, 4), nullable=True),
        sa.Column("multiplier", sa.Numeric(7, 4), server_default=sa.text("1"), nullable=False),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("adjusts_line_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_vendor_statement_lines"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_vendor_statement_lines_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "statement_id"],
            ["vendor_payable_statements.tenant_id", "vendor_payable_statements.id"],
            name="fk_vendor_statement_lines_statement_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "line_kind IN ('time', 'expense', 'adjustment')", name=op.f("ck_vendor_statement_lines_line_kind")
        ),
    )
    op.create_index("ix_vendor_statement_lines_tenant_id", ln, ["tenant_id"])
    op.create_index("ix_vendor_statement_lines_statement_id", ln, ["statement_id"])

    for table in (s, ln):
        enable_rls(table)
        enable_org_rls(table, vendor_col="vendor_id")
    enable_immutability(
        s,
        status_column="status",
        mutable_states=("draft",),
        allowed_transitions=(("finalized", "superseded"),),
    )
    enable_immutability(ln, status_column=None, parent=ParentRef(s, "statement_id", "status", ("draft",)))


def downgrade() -> None:
    for table in ("vendor_statement_lines", "vendor_payable_statements"):
        disable_immutability(table)
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
