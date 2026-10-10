# ruff: noqa: E501
"""Epic 29 216_04: ``credit_notes`` (+ lines / tax lines), the Epic 29 columns of ``debit_notes``,
``debit_note_lines`` (+ tax lines) and their immutability / period guards / org RLS (FR-150).

Revision ID: 20261005_216_04_billing_notes
Revises: 20261005_216_03_billing_invoices
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    ParentRef,
    disable_immutability,
    disable_org_rls,
    disable_period_guard,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_period_guard,
    enable_rls,
)

revision: str = "20261005_216_04_billing_notes"
down_revision: str | tuple[str, ...] | None = "20261005_216_03_billing_invoices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OPERATIONAL = (
    "delivery_status",
    "last_sent_at",
    "pdf_file_id",
    "pdf_render_version",
    "einvoice_status",
    "irn",
    "ack_no",
    "ack_at",
    "signed_qr",
    "irn_report_by",
    "irn_cancelled_at",
    "irn_cancel_reason",
)
MUTABLE = ("draft", "pending_approval")


def _document_guards(table: str, extra_transition: tuple[str, ...] = ()) -> None:
    enable_immutability(
        table,
        status_column="status",
        mutable_states=MUTABLE,
        allowed_columns=OPERATIONAL,
        allowed_transitions=(("issued", "cancelled"),),
        transition_columns=("cancel_basis", "cancelled_at", *extra_transition),
    )
    # A credit note / IRN cancellation may cancel a document of a closed month (status + cancel columns only;
    # the immutability guard still restricts the transition).
    cancel_cols = ("status", "cancel_basis", "cancelled_at", *extra_transition)
    enable_period_guard(table, date_column="doc_date", allowed_columns=(*OPERATIONAL, *cancel_cols))


def _child_guards(table: str, parent: str, fk: str) -> None:
    enable_immutability(table, status_column=None, parent=ParentRef(parent, fk, "status", MUTABLE))


def _restore_contract_guards(table: str) -> None:
    """Restore the contract-layer (20261002_200_07) configuration."""
    enable_immutability(
        table,
        status_column="status",
        mutable_states=MUTABLE,
        allowed_columns=(),
        allowed_transitions=(("issued", "cancelled"),),
        transition_columns=("cancel_basis", "cancelled_at"),
    )
    enable_period_guard(table, date_column="doc_date")


def upgrade() -> None:
    op.add_column(
        "debit_notes",
        sa.Column(
            "reason", sa.String(length=20), server_default=sa.text("'rate_correction'"), nullable=False
        ),
    )
    op.add_column(
        "debit_notes",
        sa.Column("prior_period", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("debit_notes", sa.Column("billing_profile_id", sa.Uuid(), nullable=True))
    op.add_column("debit_notes", sa.Column("billing_address_id", sa.Uuid(), nullable=True))
    op.add_column("debit_notes", sa.Column("contract_id", sa.Uuid(), nullable=True))
    op.add_column(
        "debit_notes",
        sa.Column(
            "recipient_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "debit_notes",
        sa.Column(
            "supplier_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("debit_notes", sa.Column("pos_state_code", sa.CHAR(length=2), nullable=True))
    op.add_column("debit_notes", sa.Column("supply_type", sa.String(length=12), nullable=True))
    op.add_column(
        "debit_notes", sa.Column("intra_state", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.add_column(
        "debit_notes",
        sa.Column("reverse_charge", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("debit_notes", sa.Column("lut_ref", sa.String(length=50), nullable=True))
    op.add_column("debit_notes", sa.Column("fx_rate", sa.Numeric(precision=18, scale=6), nullable=True))
    op.add_column("debit_notes", sa.Column("fx_source", sa.String(length=60), nullable=True))
    op.add_column("debit_notes", sa.Column("fx_rate_date", sa.Date(), nullable=True))
    op.add_column("debit_notes", sa.Column("preview_date", sa.Date(), nullable=True))
    op.add_column(
        "debit_notes",
        sa.Column(
            "taxable_total", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "debit_notes",
        sa.Column(
            "outside_taxable_total",
            sa.Numeric(precision=16, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "debit_notes",
        sa.Column(
            "charged_tax_total",
            sa.Numeric(precision=16, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "debit_notes",
        sa.Column(
            "rcm_tax_total", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "debit_notes",
        sa.Column(
            "round_off", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "debit_notes",
        sa.Column(
            "inr_taxable_total",
            sa.Numeric(precision=16, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "debit_notes",
        sa.Column(
            "inr_tax_total", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "debit_notes",
        sa.Column(
            "inr_document_total",
            sa.Numeric(precision=16, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column("debit_notes", sa.Column("approval_request_id", sa.Uuid(), nullable=True))
    op.add_column(
        "debit_notes", sa.Column("approved", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.add_column("debit_notes", sa.Column("preview_fingerprint", sa.CHAR(length=64), nullable=True))
    op.add_column("debit_notes", sa.Column("approved_fingerprint", sa.CHAR(length=64), nullable=True))
    op.add_column(
        "debit_notes", sa.Column("stale", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.add_column(
        "debit_notes",
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("debit_notes", sa.Column("notes", sa.Text(), nullable=True))
    op.add_column("debit_notes", sa.Column("issued_by", sa.Uuid(), nullable=True))
    op.add_column("debit_notes", sa.Column("pdf_file_id", sa.Uuid(), nullable=True))
    op.add_column(
        "debit_notes",
        sa.Column("pdf_render_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "debit_notes",
        sa.Column(
            "delivery_status", sa.String(length=10), server_default=sa.text("'not_sent'"), nullable=False
        ),
    )
    op.add_column("debit_notes", sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "debit_notes",
        sa.Column(
            "einvoice_status",
            sa.String(length=20),
            server_default=sa.text("'not_applicable'"),
            nullable=False,
        ),
    )
    op.add_column("debit_notes", sa.Column("irn", sa.CHAR(length=64), nullable=True))
    op.add_column("debit_notes", sa.Column("ack_no", sa.String(length=30), nullable=True))
    op.add_column("debit_notes", sa.Column("ack_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("debit_notes", sa.Column("signed_qr", sa.Text(), nullable=True))
    op.add_column("debit_notes", sa.Column("irn_report_by", sa.Date(), nullable=True))
    op.add_column("debit_notes", sa.Column("irn_cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("debit_notes", sa.Column("irn_cancel_reason", sa.String(length=200), nullable=True))
    op.create_table(
        "credit_notes",
        sa.Column("original_invoice_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("rebill", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("gst_adjusted", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("prior_period", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_full", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("legal_entity_id", sa.Uuid(), nullable=False),
        sa.Column("supplier_gst_registration_id", sa.Uuid(), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("series_id", sa.Uuid(), nullable=True),
        sa.Column("fiscal_year", sa.CHAR(length=5), nullable=True),
        sa.Column("number", sa.String(length=16), nullable=True),
        sa.Column("doc_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column(
            "document_total", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("content_hash", sa.CHAR(length=64), nullable=True),
        sa.Column("is_imported", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("cancel_basis", sa.String(length=20), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("billing_profile_id", sa.Uuid(), nullable=True),
        sa.Column("billing_address_id", sa.Uuid(), nullable=True),
        sa.Column("contract_id", sa.Uuid(), nullable=True),
        sa.Column(
            "recipient_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "supplier_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("pos_state_code", sa.CHAR(length=2), nullable=True),
        sa.Column("supply_type", sa.String(length=12), nullable=True),
        sa.Column("intra_state", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reverse_charge", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("lut_ref", sa.String(length=50), nullable=True),
        sa.Column("fx_rate", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("fx_source", sa.String(length=60), nullable=True),
        sa.Column("fx_rate_date", sa.Date(), nullable=True),
        sa.Column("preview_date", sa.Date(), nullable=True),
        sa.Column(
            "taxable_total", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "outside_taxable_total",
            sa.Numeric(precision=16, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "charged_tax_total",
            sa.Numeric(precision=16, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "rcm_tax_total", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "round_off", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "inr_taxable_total",
            sa.Numeric(precision=16, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "inr_tax_total", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "inr_document_total",
            sa.Numeric(precision=16, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("approved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("preview_fingerprint", sa.CHAR(length=64), nullable=True),
        sa.Column("approved_fingerprint", sa.CHAR(length=64), nullable=True),
        sa.Column("stale", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("issued_by", sa.Uuid(), nullable=True),
        sa.Column("pdf_file_id", sa.Uuid(), nullable=True),
        sa.Column("pdf_render_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "delivery_status", sa.String(length=10), server_default=sa.text("'not_sent'"), nullable=False
        ),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "einvoice_status",
            sa.String(length=20),
            server_default=sa.text("'not_applicable'"),
            nullable=False,
        ),
        sa.Column("irn", sa.CHAR(length=64), nullable=True),
        sa.Column("ack_no", sa.String(length=30), nullable=True),
        sa.Column("ack_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signed_qr", sa.Text(), nullable=True),
        sa.Column("irn_report_by", sa.Date(), nullable=True),
        sa.Column("irn_cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("irn_cancel_reason", sa.String(length=200), nullable=True),
        sa.CheckConstraint(
            "cancel_basis IS NULL OR cancel_basis IN ('irn_cancelled')",
            name=op.f("ck_credit_notes_cancel_basis"),
        ),
        sa.CheckConstraint(
            "number IS NULL OR number ~ '^[A-Za-z0-9/-]{1,16}$'", name=op.f("ck_credit_notes_number_format")
        ),
        sa.CheckConstraint(
            "reason IN ('rate_correction', 'hours_correction', 'discount', 'cancellation', 'commercial', 'other')",
            name=op.f("ck_credit_notes_reason"),
        ),
        sa.CheckConstraint(
            "status <> 'cancelled' OR cancel_basis IS NOT NULL",
            name=op.f("ck_credit_notes_cancel_basis_required"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'issued', 'discarded', 'cancelled')",
            name=op.f("ck_credit_notes_status"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('issued', 'cancelled') OR (number IS NOT NULL AND doc_date IS NOT NULL)",
            name=op.f("ck_credit_notes_issued_has_number"),
        ),
        sa.ForeignKeyConstraint(
            ["currency"],
            ["currencies.code"],
            name=op.f("fk_credit_notes_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_credit_notes_client_id_clients",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legal_entity_id"],
            ["legal_entities.tenant_id", "legal_entities.id"],
            name="fk_credit_notes_legal_entity_id_legal_entities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "original_invoice_id"],
            ["invoices.tenant_id", "invoices.id"],
            name="fk_credit_notes_original_invoice_id_invoices",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "series_id"],
            ["document_series.tenant_id", "document_series.id"],
            name="fk_credit_notes_series_id_document_series",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "supplier_gst_registration_id"],
            ["gst_registrations.tenant_id", "gst_registrations.id"],
            name="fk_credit_notes_supplier_gst_registration_id_gst_registrations",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_credit_notes_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_notes")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_credit_notes_tenant_id_id")),
    )
    op.create_index("ix_credit_notes_client_id", "credit_notes", ["client_id"], unique=False)
    op.create_index(
        "ix_credit_notes_legal_entity_doc_date", "credit_notes", ["legal_entity_id", "doc_date"], unique=False
    )
    op.create_index(
        "ix_credit_notes_original_invoice_id", "credit_notes", ["original_invoice_id"], unique=False
    )
    op.create_index(op.f("ix_credit_notes_tenant_id"), "credit_notes", ["tenant_id"], unique=False)
    op.create_index(
        "uq_credit_notes_number",
        "credit_notes",
        ["tenant_id", "series_id", "fiscal_year", "number"],
        unique=True,
        postgresql_where=sa.text("number IS NOT NULL"),
    )
    op.create_table(
        "credit_note_lines",
        sa.Column("credit_note_id", sa.Uuid(), nullable=False),
        sa.Column("original_invoice_line_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("line_no", sa.SmallInteger(), nullable=False),
        sa.Column("line_kind", sa.String(length=20), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=True),
        sa.Column("po_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("sac_code", sa.String(length=8), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("unit", sa.String(length=10), server_default=sa.text("'item'"), nullable=False),
        sa.Column("uqc", sa.String(length=8), server_default=sa.text("'OTH'"), nullable=False),
        sa.Column("rate", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column(
            "multiplier", sa.Numeric(precision=7, scale=4), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("effective_rate", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("gross_amount", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column(
            "discount_amount", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("taxable_value", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("outside_taxable_value", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "tax_rate_pct", sa.Numeric(precision=7, scale=4), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "inr_taxable_value",
            sa.Numeric(precision=16, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "cost_amount_inr", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("is_manual", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.CheckConstraint(
            "line_kind IN ('time', 'adjustment', 'expense', 'pure_agent_expense', 'milestone', 'schedule_fee', 'placement_fee', 'conversion_fee', 'manual')",
            name=op.f("ck_credit_note_lines_line_kind"),
        ),
        sa.CheckConstraint(
            "taxable_value >= 0 AND gross_amount >= 0 AND discount_amount >= 0",
            name=op.f("ck_credit_note_lines_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "credit_note_id"],
            ["credit_notes.tenant_id", "credit_notes.id"],
            name="fk_credit_note_lines_credit_note_id_credit_notes",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "original_invoice_line_id"],
            ["invoice_lines.tenant_id", "invoice_lines.id"],
            name="fk_credit_note_lines_original_line",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_credit_note_lines_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_note_lines")),
        sa.UniqueConstraint(
            "credit_note_id", "line_no", name=op.f("uq_credit_note_lines_credit_note_id_line_no")
        ),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_credit_note_lines_tenant_id_id")),
    )
    op.create_index(
        "ix_credit_note_lines_credit_note_id", "credit_note_lines", ["credit_note_id"], unique=False
    )
    op.create_index(
        "ix_credit_note_lines_original", "credit_note_lines", ["original_invoice_line_id"], unique=False
    )
    op.create_index(op.f("ix_credit_note_lines_tenant_id"), "credit_note_lines", ["tenant_id"], unique=False)
    op.create_table(
        "credit_note_tax_lines",
        sa.Column("credit_note_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("line_id", sa.Uuid(), nullable=False),
        sa.Column("component", sa.String(length=8), nullable=False),
        sa.Column("rate_pct", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.Column("amount", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("charged", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "inr_amount", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
        sa.CheckConstraint(
            "component IN ('cgst', 'sgst', 'utgst', 'igst')", name=op.f("ck_credit_note_tax_lines_component")
        ),
        sa.CheckConstraint("amount >= 0", name=op.f("ck_credit_note_tax_lines_non_negative")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "credit_note_id"],
            ["credit_notes.tenant_id", "credit_notes.id"],
            name="fk_credit_note_tax_lines_credit_note_id_credit_notes",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "line_id"],
            ["credit_note_lines.tenant_id", "credit_note_lines.id"],
            name="fk_credit_note_tax_lines_line_id_credit_note_lines",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_credit_note_tax_lines_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_note_tax_lines")),
        sa.UniqueConstraint("line_id", "component", name=op.f("uq_credit_note_tax_lines_line_id_component")),
    )
    op.create_index(
        "ix_credit_note_tax_lines_credit_note_id", "credit_note_tax_lines", ["credit_note_id"], unique=False
    )
    op.create_index(
        op.f("ix_credit_note_tax_lines_tenant_id"), "credit_note_tax_lines", ["tenant_id"], unique=False
    )
    op.create_table(
        "debit_note_lines",
        sa.Column("debit_note_id", sa.Uuid(), nullable=False),
        sa.Column("original_invoice_line_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("line_no", sa.SmallInteger(), nullable=False),
        sa.Column("line_kind", sa.String(length=20), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=True),
        sa.Column("po_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("sac_code", sa.String(length=8), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("unit", sa.String(length=10), server_default=sa.text("'item'"), nullable=False),
        sa.Column("uqc", sa.String(length=8), server_default=sa.text("'OTH'"), nullable=False),
        sa.Column("rate", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column(
            "multiplier", sa.Numeric(precision=7, scale=4), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("effective_rate", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("gross_amount", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column(
            "discount_amount", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("taxable_value", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("outside_taxable_value", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "tax_rate_pct", sa.Numeric(precision=7, scale=4), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "inr_taxable_value",
            sa.Numeric(precision=16, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "cost_amount_inr", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("is_manual", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.CheckConstraint(
            "line_kind IN ('time', 'adjustment', 'expense', 'pure_agent_expense', 'milestone', 'schedule_fee', 'placement_fee', 'conversion_fee', 'manual')",
            name=op.f("ck_debit_note_lines_line_kind"),
        ),
        sa.CheckConstraint(
            "taxable_value >= 0 AND gross_amount >= 0 AND discount_amount >= 0",
            name=op.f("ck_debit_note_lines_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "debit_note_id"],
            ["debit_notes.tenant_id", "debit_notes.id"],
            name="fk_debit_note_lines_debit_note_id_debit_notes",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "original_invoice_line_id"],
            ["invoice_lines.tenant_id", "invoice_lines.id"],
            name="fk_debit_note_lines_original_line",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_debit_note_lines_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_debit_note_lines")),
        sa.UniqueConstraint(
            "debit_note_id", "line_no", name=op.f("uq_debit_note_lines_debit_note_id_line_no")
        ),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_debit_note_lines_tenant_id_id")),
    )
    op.create_index("ix_debit_note_lines_debit_note_id", "debit_note_lines", ["debit_note_id"], unique=False)
    op.create_index(
        "ix_debit_note_lines_original", "debit_note_lines", ["original_invoice_line_id"], unique=False
    )
    op.create_index(op.f("ix_debit_note_lines_tenant_id"), "debit_note_lines", ["tenant_id"], unique=False)
    op.create_table(
        "debit_note_tax_lines",
        sa.Column("debit_note_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("line_id", sa.Uuid(), nullable=False),
        sa.Column("component", sa.String(length=8), nullable=False),
        sa.Column("rate_pct", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.Column("amount", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("charged", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "inr_amount", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
        sa.CheckConstraint(
            "component IN ('cgst', 'sgst', 'utgst', 'igst')", name=op.f("ck_debit_note_tax_lines_component")
        ),
        sa.CheckConstraint("amount >= 0", name=op.f("ck_debit_note_tax_lines_non_negative")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "debit_note_id"],
            ["debit_notes.tenant_id", "debit_notes.id"],
            name="fk_debit_note_tax_lines_debit_note_id_debit_notes",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "line_id"],
            ["debit_note_lines.tenant_id", "debit_note_lines.id"],
            name="fk_debit_note_tax_lines_line_id_debit_note_lines",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_debit_note_tax_lines_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_debit_note_tax_lines")),
        sa.UniqueConstraint("line_id", "component", name=op.f("uq_debit_note_tax_lines_line_id_component")),
    )
    op.create_index(
        "ix_debit_note_tax_lines_debit_note_id", "debit_note_tax_lines", ["debit_note_id"], unique=False
    )
    op.create_index(
        op.f("ix_debit_note_tax_lines_tenant_id"), "debit_note_tax_lines", ["tenant_id"], unique=False
    )
    enable_rls("credit_notes")
    enable_rls("credit_note_lines")
    enable_rls("credit_note_tax_lines")
    enable_rls("debit_note_lines")
    enable_rls("debit_note_tax_lines")
    _document_guards("credit_notes")
    _document_guards("debit_notes")
    enable_org_rls("credit_notes", client_col="client_id")
    _child_guards("credit_note_lines", "credit_notes", "credit_note_id")
    _child_guards("credit_note_tax_lines", "credit_notes", "credit_note_id")
    _child_guards("debit_note_lines", "debit_notes", "debit_note_id")
    _child_guards("debit_note_tax_lines", "debit_notes", "debit_note_id")
    for table in ("credit_note_lines", "credit_note_tax_lines", "debit_note_lines", "debit_note_tax_lines"):
        enable_org_rls(table)


def downgrade() -> None:
    _restore_contract_guards("debit_notes")
    disable_period_guard("credit_notes")
    disable_immutability("debit_note_tax_lines")
    disable_org_rls("debit_note_tax_lines")
    disable_rls("debit_note_tax_lines")
    op.drop_table("debit_note_tax_lines")
    disable_immutability("debit_note_lines")
    disable_org_rls("debit_note_lines")
    disable_rls("debit_note_lines")
    op.drop_table("debit_note_lines")
    disable_immutability("credit_note_tax_lines")
    disable_org_rls("credit_note_tax_lines")
    disable_rls("credit_note_tax_lines")
    op.drop_table("credit_note_tax_lines")
    disable_immutability("credit_note_lines")
    disable_org_rls("credit_note_lines")
    disable_rls("credit_note_lines")
    op.drop_table("credit_note_lines")
    disable_immutability("credit_notes")
    disable_org_rls("credit_notes")
    disable_rls("credit_notes")
    op.drop_table("credit_notes")
    op.drop_column("debit_notes", "irn_cancel_reason")
    op.drop_column("debit_notes", "irn_cancelled_at")
    op.drop_column("debit_notes", "irn_report_by")
    op.drop_column("debit_notes", "signed_qr")
    op.drop_column("debit_notes", "ack_at")
    op.drop_column("debit_notes", "ack_no")
    op.drop_column("debit_notes", "irn")
    op.drop_column("debit_notes", "einvoice_status")
    op.drop_column("debit_notes", "last_sent_at")
    op.drop_column("debit_notes", "delivery_status")
    op.drop_column("debit_notes", "pdf_render_version")
    op.drop_column("debit_notes", "pdf_file_id")
    op.drop_column("debit_notes", "issued_by")
    op.drop_column("debit_notes", "notes")
    op.drop_column("debit_notes", "warnings")
    op.drop_column("debit_notes", "stale")
    op.drop_column("debit_notes", "approved_fingerprint")
    op.drop_column("debit_notes", "preview_fingerprint")
    op.drop_column("debit_notes", "approved")
    op.drop_column("debit_notes", "approval_request_id")
    op.drop_column("debit_notes", "inr_document_total")
    op.drop_column("debit_notes", "inr_tax_total")
    op.drop_column("debit_notes", "inr_taxable_total")
    op.drop_column("debit_notes", "round_off")
    op.drop_column("debit_notes", "rcm_tax_total")
    op.drop_column("debit_notes", "charged_tax_total")
    op.drop_column("debit_notes", "outside_taxable_total")
    op.drop_column("debit_notes", "taxable_total")
    op.drop_column("debit_notes", "preview_date")
    op.drop_column("debit_notes", "fx_rate_date")
    op.drop_column("debit_notes", "fx_source")
    op.drop_column("debit_notes", "fx_rate")
    op.drop_column("debit_notes", "lut_ref")
    op.drop_column("debit_notes", "reverse_charge")
    op.drop_column("debit_notes", "intra_state")
    op.drop_column("debit_notes", "supply_type")
    op.drop_column("debit_notes", "pos_state_code")
    op.drop_column("debit_notes", "supplier_snapshot")
    op.drop_column("debit_notes", "recipient_snapshot")
    op.drop_column("debit_notes", "contract_id")
    op.drop_column("debit_notes", "billing_address_id")
    op.drop_column("debit_notes", "billing_profile_id")
    op.drop_column("debit_notes", "prior_period")
    op.drop_column("debit_notes", "reason")
