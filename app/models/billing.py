"""Billing hub tables (Epic 13 contract layer, AD-18, AD-21, AD-22, AD-36): invoices and debit notes. Epic 29
adds lines, tax lines, credit notes and the remaining columns (migrations ``20261005_216_01..04``, branch
``p2_billing``).

Guards: ``tos_immutable`` (content mutable only in ``draft`` / ``pending_approval``; ``issued -> cancelled``
may set ``cancel_basis`` / ``cancelled_at``), ``tos_period_guard`` on ``doc_date`` and org RLS by client.
Numbers come only from ``legal_entities.numbering.assign`` (AD-18).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

DOCUMENT_STATUSES = ("draft", "pending_approval", "issued", "discarded", "cancelled")
INVOICE_CANCEL_BASES = ("full_credit_note", "irn_cancelled")
DEBIT_NOTE_CANCEL_BASES = ("irn_cancelled",)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _document_args(table: str, cancel_bases: tuple[str, ...]) -> tuple[object, ...]:
    return (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", DOCUMENT_STATUSES), name="status"),
        CheckConstraint(
            "status NOT IN ('issued', 'cancelled') OR (number IS NOT NULL AND doc_date IS NOT NULL)",
            name="issued_has_number",
        ),
        CheckConstraint("status <> 'cancelled' OR cancel_basis IS NOT NULL", name="cancel_basis_required"),
        CheckConstraint(f"cancel_basis IS NULL OR {_in('cancel_basis', cancel_bases)}", name="cancel_basis"),
        CheckConstraint("number IS NULL OR number ~ '^[A-Za-z0-9/-]{1,16}$'", name="number_format"),
        Index(
            f"uq_{table}_number",
            "tenant_id",
            "series_id",
            "fiscal_year",
            "number",
            unique=True,
            postgresql_where=text("number IS NOT NULL"),
        ),
        Index(f"ix_{table}_client_id", "client_id"),
        Index(f"ix_{table}_legal_entity_doc_date", "legal_entity_id", "doc_date"),
        _tenant_fk("legal_entity_id", "legal_entities", f"fk_{table}_legal_entity_id_legal_entities"),
        _tenant_fk(
            "supplier_gst_registration_id",
            "gst_registrations",
            f"fk_{table}_supplier_gst_registration_id_gst_registrations",
        ),
        _tenant_fk("client_id", "clients", f"fk_{table}_client_id_clients"),
        _tenant_fk("series_id", "document_series", f"fk_{table}_series_id_document_series"),
    )


class _DocumentColumns:
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    supplier_gst_registration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    series_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    fiscal_year: Mapped[str | None] = mapped_column(CHAR(5), nullable=True)
    number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    currency: Mapped[str] = mapped_column(
        CHAR(3), ForeignKey("currencies.code", ondelete="RESTRICT"), nullable=False
    )
    document_total: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"))
    content_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    is_imported: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    cancel_basis: Mapped[str | None] = mapped_column(String(20), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


_MONEY = Numeric(16, 2)
_ZERO = text("0")


class _BillingColumns:
    """Epic 29 columns shared by invoices, credit notes and debit notes (216_03 / 216_04)."""

    billing_profile_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    billing_address_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    contract_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    recipient_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    supplier_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    pos_state_code: Mapped[str | None] = mapped_column(CHAR(2), nullable=True)
    supply_type: Mapped[str | None] = mapped_column(String(12), nullable=True)
    intra_state: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    reverse_charge: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    lut_ref: Mapped[str | None] = mapped_column(String(50), nullable=True)
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    fx_source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    fx_rate_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    preview_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    taxable_total: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, server_default=_ZERO)
    outside_taxable_total: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, server_default=_ZERO)
    charged_tax_total: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, server_default=_ZERO)
    rcm_tax_total: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, server_default=_ZERO)
    round_off: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, server_default=_ZERO)
    inr_taxable_total: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, server_default=_ZERO)
    inr_tax_total: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, server_default=_ZERO)
    inr_document_total: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, server_default=_ZERO)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    preview_fingerprint: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    approved_fingerprint: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    warnings: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    issued_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # operational (allow-listed after issue)
    pdf_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    pdf_render_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_ZERO)
    delivery_status: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'not_sent'")
    )
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    einvoice_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'not_applicable'")
    )
    irn: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    ack_no: Mapped[str | None] = mapped_column(String(30), nullable=True)
    ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_qr: Mapped[str | None] = mapped_column(Text, nullable=True)
    irn_report_by: Mapped[date | None] = mapped_column(Date, nullable=True)
    irn_cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    irn_cancel_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)


# Epic 51 (FR-253, migration 20261007_315_01_dsc): DSC signing state on invoices and credit notes (operational
# columns, allow-listed by the immutability trigger and the period guard; excluded from ``content_hash``).
DSC_STATUSES = ("not_required", "pending", "signed", "failed")


class _DscColumns:
    signed_pdf_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    dsc_status: Mapped[str] = mapped_column(String(15), nullable=False, server_default=text("'not_required'"))
    dsc_signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Invoice(UUIDPk, TenantMixin, Timestamps, _DocumentColumns, _BillingColumns, _DscColumns, AuditedMixin, Base):
    __tablename__ = "invoices"
    __table_args__ = (
        *_document_args("invoices", INVOICE_CANCEL_BASES),
        CheckConstraint(_in("dsc_status", DSC_STATUSES), name="dsc_status"),
    )

    invoice_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    cancelling_credit_note_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class DebitNote(UUIDPk, TenantMixin, Timestamps, _DocumentColumns, _BillingColumns, AuditedMixin, Base):
    __tablename__ = "debit_notes"
    __table_args__ = (
        *_document_args("debit_notes", DEBIT_NOTE_CANCEL_BASES),
        _tenant_fk("original_invoice_id", "invoices", "fk_debit_notes_original_invoice_id_invoices"),
        Index("ix_debit_notes_original_invoice_id", "original_invoice_id"),
    )

    original_invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'rate_correction'"))
    prior_period: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


# ================================================================ Epic 29 (branch p2_billing)
CREDIT_NOTE_REASONS = (
    "rate_correction",
    "hours_correction",
    "discount",
    "cancellation",
    "commercial",
    "other",
)
DEBIT_NOTE_REASONS = ("rate_correction", "hours_correction", "commercial", "other")
LINE_KINDS = (
    "time",
    "adjustment",
    "expense",
    "pure_agent_expense",
    "milestone",
    "schedule_fee",
    "placement_fee",
    "conversion_fee",
    "manual",
)
TAX_COMPONENTS = ("cgst", "sgst", "utgst", "igst")
SUPPLY_TYPES = ("b2b", "b2c", "sezwp", "sezwop", "expwp", "expwop", "outside_gst")
EINVOICE_STATUSES = ("not_applicable", "pending_irn", "irn_registered", "irn_cancelled")
RUN_STATUSES = ("queued", "building", "built", "failed", "discarded")


class CreditNote(UUIDPk, TenantMixin, Timestamps, _DocumentColumns, _BillingColumns, _DscColumns, AuditedMixin, Base):
    __tablename__ = "credit_notes"
    __table_args__ = (
        *_document_args("credit_notes", ("irn_cancelled",)),
        CheckConstraint(_in("dsc_status", DSC_STATUSES), name="dsc_status"),
        _tenant_fk("original_invoice_id", "invoices", "fk_credit_notes_original_invoice_id_invoices"),
        Index("ix_credit_notes_original_invoice_id", "original_invoice_id"),
        CheckConstraint(_in("reason", CREDIT_NOTE_REASONS), name="reason"),
    )

    original_invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    rebill: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    gst_adjusted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    prior_period: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    is_full: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class _LineColumns:
    line_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    line_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    po_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    sac_code: Mapped[str] = mapped_column(String(8), nullable=False)
    category: Mapped[str | None] = mapped_column(String(30), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    unit: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'item'"))
    uqc: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'OTH'"))
    rate: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False, server_default=text("1"))
    effective_rate: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, server_default=_ZERO)
    taxable_value: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    outside_taxable_value: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    tax_rate_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False, server_default=_ZERO)
    inr_taxable_value: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, server_default=_ZERO)
    cost_amount_inr: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, server_default=_ZERO)
    is_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)


def _line_args(table: str, parent: str, parent_col: str) -> tuple[object, ...]:
    return (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint(parent_col, "line_no"),
        CheckConstraint(_in("line_kind", LINE_KINDS), name="line_kind"),
        CheckConstraint(
            "taxable_value >= 0 AND gross_amount >= 0 AND discount_amount >= 0", name="non_negative"
        ),
        _tenant_fk(parent_col, parent, f"fk_{table}_{parent_col}_{parent}", ondelete="CASCADE"),
        Index(f"ix_{table}_{parent_col}", parent_col),
    )


class InvoiceLine(UUIDPk, TenantMixin, Timestamps, _LineColumns, Base):
    __tablename__ = "invoice_lines"
    __table_args__ = _line_args("invoice_lines", "invoices", "invoice_id")

    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class CreditNoteLine(UUIDPk, TenantMixin, Timestamps, _LineColumns, Base):
    __tablename__ = "credit_note_lines"
    __table_args__ = (
        *_line_args("credit_note_lines", "credit_notes", "credit_note_id"),
        _tenant_fk("original_invoice_line_id", "invoice_lines", "fk_credit_note_lines_original_line"),
        Index("ix_credit_note_lines_original", "original_invoice_line_id"),
    )

    credit_note_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    original_invoice_line_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class DebitNoteLine(UUIDPk, TenantMixin, Timestamps, _LineColumns, Base):
    __tablename__ = "debit_note_lines"
    __table_args__ = (
        *_line_args("debit_note_lines", "debit_notes", "debit_note_id"),
        _tenant_fk("original_invoice_line_id", "invoice_lines", "fk_debit_note_lines_original_line"),
        Index("ix_debit_note_lines_original", "original_invoice_line_id"),
    )

    debit_note_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    original_invoice_line_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class _TaxLineColumns:
    line_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    component: Mapped[str] = mapped_column(String(8), nullable=False)
    rate_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    charged: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    inr_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, server_default=_ZERO)


def _tax_args(table: str, parent: str, parent_col: str, line_table: str) -> tuple[object, ...]:
    return (
        UniqueConstraint("line_id", "component"),
        CheckConstraint(_in("component", TAX_COMPONENTS), name="component"),
        CheckConstraint("amount >= 0", name="non_negative"),
        _tenant_fk(parent_col, parent, f"fk_{table}_{parent_col}_{parent}", ondelete="CASCADE"),
        _tenant_fk("line_id", line_table, f"fk_{table}_line_id_{line_table}", ondelete="CASCADE"),
        Index(f"ix_{table}_{parent_col}", parent_col),
    )


class InvoiceTaxLine(UUIDPk, TenantMixin, _TaxLineColumns, Base):
    __tablename__ = "invoice_tax_lines"
    __table_args__ = _tax_args("invoice_tax_lines", "invoices", "invoice_id", "invoice_lines")

    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class CreditNoteTaxLine(UUIDPk, TenantMixin, _TaxLineColumns, Base):
    __tablename__ = "credit_note_tax_lines"
    __table_args__ = _tax_args("credit_note_tax_lines", "credit_notes", "credit_note_id", "credit_note_lines")

    credit_note_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class DebitNoteTaxLine(UUIDPk, TenantMixin, _TaxLineColumns, Base):
    __tablename__ = "debit_note_tax_lines"
    __table_args__ = _tax_args("debit_note_tax_lines", "debit_notes", "debit_note_id", "debit_note_lines")

    debit_note_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


# ---------------------------------------------------------------- tax master / settings (216_01)
class BillingSettings(Timestamps, AuditedMixin, Base):
    __tablename__ = "billing_settings"
    __table_args__ = (
        CheckConstraint("round_off_mode IN ('none', 'nearest_rupee')", name="round_off_mode"),
        CheckConstraint("cn_gst_deadline ~ '^[0-1][0-9]-[0-3][0-9]$'", name="cn_gst_deadline"),
    )
    __audit_entity_id__ = "tenant_id"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    round_off_mode: Mapped[str] = mapped_column(String(15), nullable=False, server_default=text("'none'"))
    cn_gst_deadline: Mapped[str] = mapped_column(CHAR(5), nullable=False, server_default=text("'11-30'"))
    allow_backdated_issue: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    stale_unbilled_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))
    pending_irn_alert_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("24"))
    annexure_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class BillingClientSettings(Timestamps, AuditedMixin, Base):
    __tablename__ = "billing_client_settings"
    __table_args__ = (
        _tenant_fk(
            "client_id", "clients", "fk_billing_client_settings_client_id_clients", ondelete="CASCADE"
        ),
    )
    __audit_entity_id__ = "client_id"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    reverse_charge: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    annexure: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class SacCode(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "sac_codes"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)

    code: Mapped[str] = mapped_column(String(8), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    default_for_line_kind: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class TaxRate(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "tax_rates"
    __table_args__ = (
        CheckConstraint("rate_pct >= 0 AND rate_pct <= 100", name="rate_pct"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="dates"),
        Index("ix_tax_rates_sac_code", "tenant_id", "sac_code"),
    )

    sac_code: Mapped[str] = mapped_column(String(8), nullable=False)
    rate_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)


# ---------------------------------------------------------------- rules, items, runs (216_02)
class BillingRule(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "billing_rules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "placement_id"),
        CheckConstraint(
            "basis IN ('hourly', 'daily', 'monthly_fixed', 'milestone', 'fixed_fee')", name="basis"
        ),
        CheckConstraint("proration_days IN ('working', 'calendar')", name="proration_days"),
        CheckConstraint("expense_rebill IN ('at_cost', 'markup', 'pure_agent')", name="expense_rebill"),
        CheckConstraint("grouping IN ('per_placement', 'per_po', 'per_address_period')", name="grouping"),
        CheckConstraint("frequency IN ('weekly', 'monthly', 'on_milestone')", name="frequency"),
        _tenant_fk(
            "placement_id", "placements", "fk_billing_rules_placement_id_placements", ondelete="CASCADE"
        ),
    )

    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    basis: Mapped[str] = mapped_column(String(15), nullable=False, server_default=text("'hourly'"))
    prorate: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    proration_days: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'working'"))
    billable_categories: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    expense_rebill: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'at_cost'"))
    markup_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)
    grouping: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'per_placement'"))
    frequency: Mapped[str] = mapped_column(String(15), nullable=False, server_default=text("'monthly'"))
    line_template: Mapped[str | None] = mapped_column(String(300), nullable=True)
    sac_code: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'998513'"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class BillingScheduleItem(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "billing_schedule_items"
    __table_args__ = (
        UniqueConstraint("tenant_id", "placement_id", "kind", "period_start"),
        CheckConstraint("kind IN ('monthly_fixed', 'scheduled_fee', 'conversion_fee')", name="kind"),
        CheckConstraint("amount >= 0", name="amount"),
        _tenant_fk("placement_id", "placements", "fk_billing_schedule_items_placement_id_placements"),
    )

    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class ManualBillingItem(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "manual_billing_items"
    __table_args__ = (
        CheckConstraint("kind IN ('one_off', 'placement_fee', 'milestone')", name="kind"),
        CheckConstraint("quantity > 0 AND rate >= 0", name="amounts"),
        Index(
            "uq_manual_billing_items_offer",
            "tenant_id",
            "offer_id",
            unique=True,
            postgresql_where=text("kind = 'placement_fee'"),
        ),
        _tenant_fk("client_id", "clients", "fk_manual_billing_items_client_id_clients"),
        _tenant_fk(
            "legal_entity_id", "legal_entities", "fk_manual_billing_items_legal_entity_id_legal_entities"
        ),
        _tenant_fk("placement_id", "placements", "fk_manual_billing_items_placement_id_placements"),
    )

    kind: Mapped[str] = mapped_column(String(15), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    offer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    item_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    sac_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    po_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class InvoiceRun(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "invoice_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        CheckConstraint("period_end >= period_start", name="period"),
        _tenant_fk("legal_entity_id", "legal_entities", "fk_invoice_runs_legal_entity_id_legal_entities"),
        _tenant_fk(
            "gst_registration_id",
            "gst_registrations",
            "fk_invoice_runs_gst_registration_id_gst_registrations",
        ),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    gst_registration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    placement_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid(as_uuid=True)), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'queued'"))
    progress_pct: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=_ZERO)
    draft_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_ZERO)
    totals: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class InvoiceRunException(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "invoice_run_exceptions"
    __table_args__ = (
        _tenant_fk(
            "run_id", "invoice_runs", "fk_invoice_run_exceptions_run_id_invoice_runs", ondelete="CASCADE"
        ),
        Index("ix_invoice_run_exceptions_run_id", "run_id"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'error'"))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class EinvoiceImport(UUIDPk, TenantMixin, Timestamps, Base):
    """A portal response file (IRN / Ack / QR per document): previewed, then applied once (sha256 dedupe)."""

    __tablename__ = "einvoice_imports"
    __table_args__ = (
        CheckConstraint("status IN ('previewed', 'applied', 'discarded')", name="status"),
        Index(
            "uq_einvoice_imports_applied_sha",
            "tenant_id",
            "sha256",
            unique=True,
            postgresql_where=text("status = 'applied'"),
        ),
    )

    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'previewed'"))
    rows: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


# ================================================================ Epic 51 (branch p3_fin_carry): DSC signing
class DscPolicy(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """DSC requirement per GST Registration and Client (NULL Client = every Client of the registration).
    Resolution: exact Client row, else the NULL-Client row, else no policy."""

    __tablename__ = "dsc_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("gst_registration_id", "gst_registrations", "fk_dsc_policies_gst_registration_id_gst_registrations"),
        _tenant_fk("client_id", "clients", "fk_dsc_policies_client_id_clients"),
        _tenant_fk("provider_config_id", "provider_connector_configs", "fk_dsc_policies_provider_config_id"),
        Index(
            "uq_dsc_policies_registration_client",
            "tenant_id",
            "gst_registration_id",
            text("COALESCE(client_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
        ),
    )

    gst_registration_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    provider_config_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    signer_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reason: Mapped[str] = mapped_column(String(200), nullable=False, server_default=text("'Invoice issued'"))
    location: Mapped[str | None] = mapped_column(String(100), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class DscSignRequest(UUIDPk, TenantMixin, Timestamps, Base):
    """One signing request per issued document (``kind``, ``doc_id``): the stored render, the signed file (document
    version 2) and the verification summary (never the PDF bytes)."""

    __tablename__ = "dsc_sign_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "kind", "doc_id", name="uq_dsc_sign_requests_kind_doc"),
        CheckConstraint("kind IN ('invoice', 'credit_note')", name="kind"),
        CheckConstraint("status IN ('pending', 'signed', 'failed')", name="status"),
        CheckConstraint("attempts >= 0", name="attempts"),
        _tenant_fk("policy_id", "dsc_policies", "fk_dsc_sign_requests_policy_id_dsc_policies"),
        _tenant_fk("render_file_id", "stored_files", "fk_dsc_sign_requests_render_file_id_stored_files"),
        _tenant_fk("signed_file_id", "stored_files", "fk_dsc_sign_requests_signed_file_id_stored_files"),
        _tenant_fk("document_id", "documents", "fk_dsc_sign_requests_document_id_documents"),
        Index("ix_dsc_sign_requests_tenant_status", "tenant_id", "status"),
    )

    kind: Mapped[str] = mapped_column(String(15), nullable=False)
    doc_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    policy_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'pending'"))
    adapter_key: Mapped[str] = mapped_column(String(40), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_ZERO)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    render_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    render_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    signed_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    signer_subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    signer_serial: Mapped[str | None] = mapped_column(String(100), nullable=True)
    signing_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
