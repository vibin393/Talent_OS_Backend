# ruff: noqa: E501
"""Epic 30 217_01: ``receivables_settings``, ``payments``, ``allocations``, ``allocation_reversals``,
``client_credits``, ``invoice_balances``, ``bank_statement_rows`` (FR-153, AD-21, AD-22, AD-32).

Guards: payments / allocations / reversals / client credits are immutable (payments: allocation and reversal
markers only) and period-guarded on their date column. Org RLS (client) on the portal-read tables
(``payments``, ``allocations``, ``client_credits``, ``invoice_balances``).

Revision ID: 20261005_217_01_rcv_payments
Revises: 20261002_200_07
Create Date: 2026-10-05
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    disable_immutability,
    disable_org_rls,
    disable_period_guard,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_period_guard,
    enable_rls,
)

revision: str = "20261005_217_01_rcv_payments"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_receivables",)
depends_on: str | Sequence[str] | None = "20261005_216_04_billing_notes"

PAYMENT_MARKERS = ("allocation_status", "reversed_at", "reversal_reason", "reversal_date")


def base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"),
    ]


def tfk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


def money(name: str, nullable: bool = False, default: str | None = None) -> sa.Column:
    return sa.Column(name, sa.Numeric(16, 2), nullable=nullable, server_default=sa.text(default) if default else None)


def tenant_index(table: str) -> None:
    op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"], unique=False)


ONE_TARGET = "(invoice_id IS NOT NULL) <> (debit_note_id IS NOT NULL)"


def upgrade() -> None:
    op.create_table(
        "receivables_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("aging_buckets", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[30, 60, 90]'::jsonb"), nullable=False),
        sa.Column("auto_allocate_order", sa.String(30), server_default=sa.text("'oldest_due_first'"), nullable=False),
        money("tds_variance_tolerance", default="1.00"),
        sa.Column("ptp_grace_days", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("reminder_rules_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_receivables_settings"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_receivables_settings_tenant_id_tenants", ondelete="RESTRICT"),
    )

    op.create_table(
        "payments",
        *base("payments"),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        money("amount"),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("fx_rate", sa.Numeric(18, 6), nullable=True),
        sa.Column("fx_source", sa.String(60), nullable=True),
        money("inr_amount"),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("reference", sa.String(120), nullable=True),
        sa.Column("receiving_bank_account_id", sa.Uuid(), nullable=True),
        sa.Column("allocation_status", sa.String(24), server_default=sa.text("'unallocated'"), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversal_reason", sa.String(500), nullable=True),
        sa.Column("reversal_date", sa.Date(), nullable=True),
        sa.Column("prior_period", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("import_batch_id", sa.Uuid(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_payments_tenant_id_id"),
        sa.CheckConstraint("mode IN ('neft', 'rtgs', 'imps', 'upi', 'cheque', 'wire')", name="ck_payments_mode"),
        sa.CheckConstraint(
            "allocation_status IN ('unallocated', 'partially_allocated', 'fully_allocated', 'reversed')",
            name="ck_payments_allocation_status",
        ),
        sa.CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        sa.CheckConstraint("currency = 'INR' OR fx_rate IS NOT NULL", name="ck_payments_fx_required"),
        tfk("legal_entity_id", "legal_entities", "fk_payments_legal_entity_id_legal_entities"),
        tfk("client_id", "clients", "fk_payments_client_id_clients"),
    )
    tenant_index("payments")
    op.create_index("ix_payments_client_date", "payments", ["tenant_id", "client_id", "payment_date"])
    op.create_index("ix_payments_le_date", "payments", ["tenant_id", "legal_entity_id", "payment_date"])
    op.create_index("ix_payments_reference", "payments", ["tenant_id", "legal_entity_id", "reference"])

    op.create_table(
        "allocations",
        *base("allocations"),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.String(12), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("credit_note_id", sa.Uuid(), nullable=True),
        sa.Column("invoice_id", sa.Uuid(), nullable=True),
        sa.Column("debit_note_id", sa.Uuid(), nullable=True),
        sa.Column("allocation_date", sa.Date(), nullable=False),
        money("amount"),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        money("inr_at_document"),
        money("inr_at_payment"),
        money("realized_fx"),
        sa.Column("applied_from_credit", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("prior_period", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_allocations_tenant_id_id"),
        sa.CheckConstraint("source_kind IN ('payment', 'credit_note')", name="ck_allocations_source_kind"),
        sa.CheckConstraint(
            "(source_kind = 'payment' AND payment_id IS NOT NULL AND credit_note_id IS NULL) OR "
            "(source_kind = 'credit_note' AND credit_note_id IS NOT NULL AND payment_id IS NULL)",
            name="ck_allocations_source_ref",
        ),
        sa.CheckConstraint(ONE_TARGET, name="ck_allocations_one_target"),
        sa.CheckConstraint("amount > 0", name="ck_allocations_amount_positive"),
        tfk("legal_entity_id", "legal_entities", "fk_allocations_legal_entity_id_legal_entities"),
        tfk("payment_id", "payments", "fk_allocations_payment_id_payments"),
        tfk("invoice_id", "invoices", "fk_allocations_invoice_id_invoices"),
        tfk("debit_note_id", "debit_notes", "fk_allocations_debit_note_id_debit_notes"),
    )
    tenant_index("allocations")
    op.create_index("ix_allocations_invoice", "allocations", ["tenant_id", "invoice_id", "allocation_date"])
    op.create_index("ix_allocations_debit_note", "allocations", ["tenant_id", "debit_note_id", "allocation_date"])
    op.create_index("ix_allocations_payment", "allocations", ["tenant_id", "payment_id"])
    op.create_index("ix_allocations_client_date", "allocations", ["tenant_id", "client_id", "allocation_date"])
    op.create_index(
        "uq_allocations_credit_note_invoice",
        "allocations",
        ["tenant_id", "credit_note_id", "invoice_id"],
        unique=True,
        postgresql_where=sa.text("source_kind = 'credit_note' AND applied_from_credit = false"),
    )

    op.create_table(
        "allocation_reversals",
        *base("allocation_reversals"),
        sa.Column("allocation_id", sa.Uuid(), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("reversal_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        money("amount"),
        money("inr_at_document"),
        money("inr_at_payment"),
        money("realized_fx"),
        sa.Column("prior_period", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "allocation_id", name="uq_allocation_reversals_allocation"),
        tfk("allocation_id", "allocations", "fk_allocation_reversals_allocation_id_allocations"),
        tfk("legal_entity_id", "legal_entities", "fk_allocation_reversals_le_legal_entities"),
    )
    tenant_index("allocation_reversals")

    op.create_table(
        "client_credits",
        *base("client_credits"),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("credit_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        money("amount"),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("credit_note_id", sa.Uuid(), nullable=True),
        sa.Column("allocation_id", sa.Uuid(), nullable=True),
        sa.Column("prior_period", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('payment_remainder', 'credit_note_excess', 'applied', 'reversal')", name="ck_client_credits_kind"
        ),
        sa.CheckConstraint("amount <> 0", name="ck_client_credits_amount_nonzero"),
        tfk("legal_entity_id", "legal_entities", "fk_client_credits_legal_entity_id_legal_entities"),
        tfk("client_id", "clients", "fk_client_credits_client_id_clients"),
        tfk("payment_id", "payments", "fk_client_credits_payment_id_payments"),
        tfk("allocation_id", "allocations", "fk_client_credits_allocation_id_allocations"),
    )
    tenant_index("client_credits")
    op.create_index("ix_client_credits_client", "client_credits", ["tenant_id", "client_id", "currency", "credit_date"])

    op.create_table(
        "invoice_balances",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("receivable_id", sa.Uuid(), nullable=False),
        sa.Column("receivable_kind", sa.String(12), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        money("document_total"),
        money("open_amount"),
        sa.Column("payment_status", sa.String(16), nullable=False),
        money("disputed_amount", default="0"),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("rebuilt_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("receivable_id", name="pk_invoice_balances"),
        sa.CheckConstraint("receivable_kind IN ('invoice', 'debit_note')", name="ck_invoice_balances_kind"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_invoice_balances_tenant_id_tenants", ondelete="RESTRICT"),
    )
    tenant_index("invoice_balances")
    op.create_index("ix_invoice_balances_aging", "invoice_balances", ["tenant_id", "legal_entity_id", "client_id", "due_date"])

    op.create_table(
        "bank_statement_rows",
        *base("bank_statement_rows"),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("bank_account_id", sa.Uuid(), nullable=True),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=True),
        money("amount", nullable=True),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'INR'"), nullable=False),
        sa.Column("reference", sa.String(200), nullable=True),
        sa.Column("narrative", sa.String(500), nullable=True),
        sa.Column("match_kind", sa.String(16), nullable=False),
        sa.Column("matched_document_ids", postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("soft_duplicate_of", sa.Uuid(), nullable=True),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("error", sa.String(300), nullable=True),
        sa.CheckConstraint(
            "match_kind IN ('matched', 'unmatched', 'soft_duplicate', 'applied', 'failed')",
            name="ck_bank_statement_rows_match_kind",
        ),
        sa.UniqueConstraint("tenant_id", "batch_id", "row_no", name="uq_bank_statement_rows_batch_row"),
        tfk("legal_entity_id", "legal_entities", "fk_bank_statement_rows_le_legal_entities"),
        sa.ForeignKeyConstraint(["batch_id"], ["file_import_batches.id"], name="fk_bank_statement_rows_batch_id_batches", ondelete="RESTRICT"),
    )
    tenant_index("bank_statement_rows")

    for table in ("receivables_settings", "payments", "allocations", "allocation_reversals", "client_credits", "invoice_balances", "bank_statement_rows"):
        enable_rls(table)
    for table in ("payments", "allocations", "client_credits", "invoice_balances"):
        enable_org_rls(table, client_col="client_id")

    enable_immutability("payments", status_column=None, allowed_columns=PAYMENT_MARKERS)
    enable_period_guard("payments", date_column="payment_date", allowed_columns=PAYMENT_MARKERS)
    for table, date_column in (("allocations", "allocation_date"), ("allocation_reversals", "reversal_date"), ("client_credits", "credit_date")):
        enable_immutability(table, status_column=None)
        enable_period_guard(table, date_column=date_column)


def downgrade() -> None:
    for table in ("payments", "allocations", "allocation_reversals", "client_credits"):
        disable_period_guard(table)
        disable_immutability(table)
    for table in ("payments", "allocations", "client_credits", "invoice_balances"):
        disable_org_rls(table)
    for table in ("bank_statement_rows", "invoice_balances", "client_credits", "allocation_reversals", "allocations", "payments", "receivables_settings"):
        disable_rls(table)
        op.drop_table(table)
