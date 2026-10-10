# ruff: noqa: E501
"""Epic 16 203_04: purchase order business columns, the append-only consumption ledger and PO alerts (FR-92, AD-32).

ALTERs the contract-layer hub table ``purchase_orders`` (keys / org columns untouched; PO numbers become unique
case-insensitively per client), adds ``po_consumptions`` (written only by ``record_po_consumption``; UPDATE /
DELETE refused by trigger) and ``po_alerts`` (threshold / expiry / validity alert dedupe).

Revision ID: 20261003_203_04_po_ledger
Revises: 20261003_203_03_rate_cards
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    InheritRef,
    disable_immutability,
    disable_org_rls,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_rls,
)

revision: str = "20261003_203_04_po_ledger"
down_revision: str | tuple[str, ...] | None = "20261003_203_03_rate_cards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS: list[sa.Column] = [  # type: ignore[type-arg]
    sa.Column("owner_id", sa.Uuid(), nullable=True),
    sa.Column("file_id", sa.Uuid(), nullable=True),
    sa.Column("alert_thresholds_pct", postgresql.ARRAY(sa.Integer()), nullable=True),
    sa.Column("notes", sa.Text(), nullable=True),
    sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
]
FKS: tuple[tuple[str, str], ...] = (("owner_id", "users"), ("file_id", "stored_files"))


def tfk(table: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=f"fk_{table}_{column}_{target}", ondelete=ondelete
    )


def upgrade() -> None:
    for column in COLUMNS:
        op.add_column("purchase_orders", column)
    for column, target in FKS:
        op.create_foreign_key(
            f"fk_purchase_orders_{column}_{target}", "purchase_orders", target, ["tenant_id", column], ["tenant_id", "id"], ondelete="RESTRICT"
        )
    op.create_check_constraint("ck_purchase_orders_some_cap", "purchase_orders", "value_cap IS NOT NULL OR hours_cap IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX uq_purchase_orders_number_ci ON purchase_orders (tenant_id, client_id, lower(po_number))")

    op.create_table(
        "po_consumptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("po_id", sa.Uuid(), nullable=False),
        sa.Column("source_doc_kind", sa.String(12), nullable=False),
        sa.Column("source_doc_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False),
        sa.Column("hours", sa.Numeric(10, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("exception_approval_id", sa.Uuid(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_po_consumptions"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_po_consumptions_tenant_id_tenants", ondelete="RESTRICT"),
        sa.CheckConstraint("source_doc_kind IN ('invoice', 'credit_note', 'debit_note')", name="ck_po_consumptions_source_doc_kind"),
        tfk("po_consumptions", "client_id", "clients"),
        tfk("po_consumptions", "po_id", "purchase_orders"),
        sa.UniqueConstraint("source_doc_kind", "source_doc_id", "po_id", name="uq_po_consumptions_source_doc_kind_source_doc_id_po_id"),
    )
    op.create_index("ix_po_consumptions_tenant_id", "po_consumptions", ["tenant_id"])
    op.create_index("ix_po_consumptions_po_id", "po_consumptions", ["po_id"])

    op.create_table(
        "po_alerts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("po_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("crossing_seq", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_po_alerts"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_po_alerts_tenant_id_tenants", ondelete="RESTRICT"),
        sa.CheckConstraint("kind IN ('threshold', 'expiring', 'validity')", name="ck_po_alerts_kind"),
        tfk("po_alerts", "po_id", "purchase_orders", "CASCADE"),
        sa.UniqueConstraint("po_id", "kind", "threshold", "crossing_seq", name="uq_po_alerts_po_id_kind_threshold_crossing_seq"),
    )
    op.create_index("ix_po_alerts_tenant_id", "po_alerts", ["tenant_id"])

    enable_rls("po_consumptions")
    enable_rls("po_alerts")
    enable_org_rls(
        "po_consumptions", client_col="client_id", inherit_from=InheritRef("purchase_orders", "po_id", ("client_id",))
    )
    enable_org_rls("po_alerts")  # internal-only
    enable_immutability("po_consumptions", status_column=None, mutable_states=())


def downgrade() -> None:
    disable_immutability("po_consumptions")
    for table in ("po_alerts", "po_consumptions"):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
    op.execute("DROP INDEX IF EXISTS uq_purchase_orders_number_ci")
    op.drop_constraint("ck_purchase_orders_some_cap", "purchase_orders", type_="check")
    for column, target in FKS:
        op.drop_constraint(f"fk_purchase_orders_{column}_{target}", "purchase_orders", type_="foreignkey")
    for column in reversed(COLUMNS):
        op.drop_column("purchase_orders", column.name)
