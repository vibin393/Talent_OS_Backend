# ruff: noqa: E501
"""Epic 29 216_03: the Epic 29 columns of the contract-layer ``invoices`` table, ``invoice_lines``,
``invoice_tax_lines`` and ``einvoice_imports`` (FR-147/148/151).

Guards (AD-21/22): invoices are content-immutable once issued; only the operational columns (delivery, PDF,
e-invoice) change afterwards and ``issued -> cancelled`` may set the cancel columns. Lines / tax lines follow
the parent's status (INSERT/UPDATE/DELETE guard, parent read FOR SHARE). Period guard on ``doc_date``.

Revision ID: 20261005_216_03_billing_invoices
Revises: 20261005_216_02_billing_rules
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
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_period_guard,
    enable_rls,
)

revision: str = "20261005_216_03_billing_invoices"
down_revision: str | tuple[str, ...] | None = "20261005_216_02_billing_rules"
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
    op.add_column("invoices", sa.Column("invoice_run_id", sa.Uuid(), nullable=True))
    op.add_column("invoices", sa.Column("cancelling_credit_note_id", sa.Uuid(), nullable=True))
    op.add_column("invoices", sa.Column("billing_profile_id", sa.Uuid(), nullable=True))
    op.add_column("invoices", sa.Column("billing_address_id", sa.Uuid(), nullable=True))
    op.add_column("invoices", sa.Column("contract_id", sa.Uuid(), nullable=True))
    op.add_column(
        "invoices",
        sa.Column(
            "recipient_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "supplier_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("invoices", sa.Column("pos_state_code", sa.CHAR(length=2), nullable=True))
    op.add_column("invoices", sa.Column("supply_type", sa.String(length=12), nullable=True))
    op.add_column(
        "invoices", sa.Column("intra_state", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.add_column(
        "invoices", sa.Column("reverse_charge", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.add_column("invoices", sa.Column("lut_ref", sa.String(length=50), nullable=True))
    op.add_column("invoices", sa.Column("fx_rate", sa.Numeric(precision=18, scale=6), nullable=True))
    op.add_column("invoices", sa.Column("fx_source", sa.String(length=60), nullable=True))
    op.add_column("invoices", sa.Column("fx_rate_date", sa.Date(), nullable=True))
    op.add_column("invoices", sa.Column("preview_date", sa.Date(), nullable=True))
    op.add_column(
        "invoices",
        sa.Column(
            "taxable_total", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "outside_taxable_total",
            sa.Numeric(precision=16, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "charged_tax_total",
            sa.Numeric(precision=16, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "rcm_tax_total", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "round_off", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "inr_taxable_total",
            sa.Numeric(precision=16, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "inr_tax_total", sa.Numeric(precision=16, scale=2), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "inr_document_total",
            sa.Numeric(precision=16, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column("invoices", sa.Column("approval_request_id", sa.Uuid(), nullable=True))
    op.add_column(
        "invoices", sa.Column("approved", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.add_column("invoices", sa.Column("preview_fingerprint", sa.CHAR(length=64), nullable=True))
    op.add_column("invoices", sa.Column("approved_fingerprint", sa.CHAR(length=64), nullable=True))
    op.add_column(
        "invoices", sa.Column("stale", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.add_column(
        "invoices",
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("invoices", sa.Column("notes", sa.Text(), nullable=True))
    op.add_column("invoices", sa.Column("issued_by", sa.Uuid(), nullable=True))
    op.add_column("invoices", sa.Column("pdf_file_id", sa.Uuid(), nullable=True))
    op.add_column(
        "invoices", sa.Column("pdf_render_version", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )
    op.add_column(
        "invoices",
        sa.Column(
            "delivery_status", sa.String(length=10), server_default=sa.text("'not_sent'"), nullable=False
        ),
    )
    op.add_column("invoices", sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "invoices",
        sa.Column(
            "einvoice_status",
            sa.String(length=20),
            server_default=sa.text("'not_applicable'"),
            nullable=False,
        ),
    )
    op.add_column("invoices", sa.Column("irn", sa.CHAR(length=64), nullable=True))
    op.add_column("invoices", sa.Column("ack_no", sa.String(length=30), nullable=True))
    op.add_column("invoices", sa.Column("ack_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("invoices", sa.Column("signed_qr", sa.Text(), nullable=True))
    op.add_column("invoices", sa.Column("irn_report_by", sa.Date(), nullable=True))
    op.add_column("invoices", sa.Column("irn_cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("invoices", sa.Column("irn_cancel_reason", sa.String(length=200), nullable=True))
    op.create_table(
        "invoice_lines",
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
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
            name=op.f("ck_invoice_lines_line_kind"),
        ),
        sa.CheckConstraint(
            "taxable_value >= 0 AND gross_amount >= 0 AND discount_amount >= 0",
            name=op.f("ck_invoice_lines_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "invoice_id"],
            ["invoices.tenant_id", "invoices.id"],
            name="fk_invoice_lines_invoice_id_invoices",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_invoice_lines_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_lines")),
        sa.UniqueConstraint("invoice_id", "line_no", name=op.f("uq_invoice_lines_invoice_id_line_no")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_invoice_lines_tenant_id_id")),
    )
    op.create_index("ix_invoice_lines_invoice_id", "invoice_lines", ["invoice_id"], unique=False)
    op.create_index(op.f("ix_invoice_lines_tenant_id"), "invoice_lines", ["tenant_id"], unique=False)
    op.create_table(
        "invoice_tax_lines",
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
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
            "component IN ('cgst', 'sgst', 'utgst', 'igst')", name=op.f("ck_invoice_tax_lines_component")
        ),
        sa.CheckConstraint("amount >= 0", name=op.f("ck_invoice_tax_lines_non_negative")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "invoice_id"],
            ["invoices.tenant_id", "invoices.id"],
            name="fk_invoice_tax_lines_invoice_id_invoices",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "line_id"],
            ["invoice_lines.tenant_id", "invoice_lines.id"],
            name="fk_invoice_tax_lines_line_id_invoice_lines",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_invoice_tax_lines_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_tax_lines")),
        sa.UniqueConstraint("line_id", "component", name=op.f("uq_invoice_tax_lines_line_id_component")),
    )
    op.create_index("ix_invoice_tax_lines_invoice_id", "invoice_tax_lines", ["invoice_id"], unique=False)
    op.create_index(op.f("ix_invoice_tax_lines_tenant_id"), "invoice_tax_lines", ["tenant_id"], unique=False)
    op.create_table(
        "einvoice_imports",
        sa.Column("sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'previewed'"), nullable=False),
        sa.Column(
            "rows",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('previewed', 'applied', 'discarded')", name=op.f("ck_einvoice_imports_status")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_einvoice_imports_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_einvoice_imports")),
    )
    op.create_index(op.f("ix_einvoice_imports_tenant_id"), "einvoice_imports", ["tenant_id"], unique=False)
    op.create_index(
        "uq_einvoice_imports_applied_sha",
        "einvoice_imports",
        ["tenant_id", "sha256"],
        unique=True,
        postgresql_where=sa.text("status = 'applied'"),
    )
    enable_rls("invoice_lines")
    enable_rls("invoice_tax_lines")
    enable_rls("einvoice_imports")
    _document_guards("invoices", ("cancelling_credit_note_id",))
    _child_guards("invoice_lines", "invoices", "invoice_id")
    _child_guards("invoice_tax_lines", "invoices", "invoice_id")
    for table in ("invoice_lines", "invoice_tax_lines", "einvoice_imports"):
        enable_org_rls(table)


def downgrade() -> None:
    _restore_contract_guards("invoices")
    disable_immutability("einvoice_imports")
    disable_org_rls("einvoice_imports")
    disable_rls("einvoice_imports")
    op.drop_table("einvoice_imports")
    disable_immutability("invoice_tax_lines")
    disable_org_rls("invoice_tax_lines")
    disable_rls("invoice_tax_lines")
    op.drop_table("invoice_tax_lines")
    disable_immutability("invoice_lines")
    disable_org_rls("invoice_lines")
    disable_rls("invoice_lines")
    op.drop_table("invoice_lines")
    op.drop_column("invoices", "irn_cancel_reason")
    op.drop_column("invoices", "irn_cancelled_at")
    op.drop_column("invoices", "irn_report_by")
    op.drop_column("invoices", "signed_qr")
    op.drop_column("invoices", "ack_at")
    op.drop_column("invoices", "ack_no")
    op.drop_column("invoices", "irn")
    op.drop_column("invoices", "einvoice_status")
    op.drop_column("invoices", "last_sent_at")
    op.drop_column("invoices", "delivery_status")
    op.drop_column("invoices", "pdf_render_version")
    op.drop_column("invoices", "pdf_file_id")
    op.drop_column("invoices", "issued_by")
    op.drop_column("invoices", "notes")
    op.drop_column("invoices", "warnings")
    op.drop_column("invoices", "stale")
    op.drop_column("invoices", "approved_fingerprint")
    op.drop_column("invoices", "preview_fingerprint")
    op.drop_column("invoices", "approved")
    op.drop_column("invoices", "approval_request_id")
    op.drop_column("invoices", "inr_document_total")
    op.drop_column("invoices", "inr_tax_total")
    op.drop_column("invoices", "inr_taxable_total")
    op.drop_column("invoices", "round_off")
    op.drop_column("invoices", "rcm_tax_total")
    op.drop_column("invoices", "charged_tax_total")
    op.drop_column("invoices", "outside_taxable_total")
    op.drop_column("invoices", "taxable_total")
    op.drop_column("invoices", "preview_date")
    op.drop_column("invoices", "fx_rate_date")
    op.drop_column("invoices", "fx_source")
    op.drop_column("invoices", "fx_rate")
    op.drop_column("invoices", "lut_ref")
    op.drop_column("invoices", "reverse_charge")
    op.drop_column("invoices", "intra_state")
    op.drop_column("invoices", "supply_type")
    op.drop_column("invoices", "pos_state_code")
    op.drop_column("invoices", "supplier_snapshot")
    op.drop_column("invoices", "recipient_snapshot")
    op.drop_column("invoices", "contract_id")
    op.drop_column("invoices", "billing_address_id")
    op.drop_column("invoices", "billing_profile_id")
    op.drop_column("invoices", "cancelling_credit_note_id")
    op.drop_column("invoices", "invoice_run_id")
