# ruff: file-ignore[line-too-long]
"""Receivables, collections and period close tables (Epic 30, FR-153..FR-161). Branch ``p2_receivables``,
migrations ``20261005_217_01..03``.

Settlement facts (AD-21, AD-22, AD-33) are append-only, dated and period-guarded: ``payments`` (only the
allocation / reversal markers change), ``allocations`` + ``allocation_reversals``, ``client_credits``,
``tds_deductions`` (certificate fields change) + ``tds_deduction_reversals``, ``write_offs`` +
``write_off_reversals``. Corrections are compensating reversal rows whose values are copied from the original and
negated, never recomputed. ``invoice_balances`` is a read cache only (rebuilt nightly, never used for guards).

Operational (mutable, audited): settings, bank statement rows, write-off requests, disputes, collection activities,
client collection settings, export templates / exports, reconciliation runs / findings, period close events.

Money is NUMERIC(16,2), FX NUMERIC(18,6), TDS rates NUMERIC(7,4) (AD-20).
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
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

MONEY = Numeric(16, 2)
FX = Numeric(18, 6)
RATE = Numeric(7, 4)
PAYMENT_MODES = ("neft", "rtgs", "imps", "upi", "cheque", "wire")
ALLOCATION_STATUSES = ("unallocated", "partially_allocated", "fully_allocated", "reversed")
CREDIT_KINDS = ("payment_remainder", "credit_note_excess", "applied", "reversal")
TDS_KINDS = ("income_tax", "gst_tds")
CERT_STATUSES = ("expected", "received", "mismatch")
WRITE_OFF_STATUSES = ("pending_approval", "approved", "rejected", "executed", "lapsed")
DISPUTE_STATUSES = ("open", "resolved_credit_note", "resolved_reissue", "rejected")
ACTIVITY_KINDS = ("call", "email", "meeting", "note", "promise_to_pay")
PTP_STATUSES = ("open", "kept", "broken")
CLOSE_ACTIONS = ("close", "reopen_requested", "reopened", "reclosed", "reopen_rejected")
BANK_MATCH_KINDS = ("matched", "unmatched", "soft_duplicate", "applied", "failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


_ONE_TARGET = "(invoice_id IS NOT NULL) <> (debit_note_id IS NOT NULL)"


class ReceivablesSettings(Timestamps, AuditedMixin, Base):
    __tablename__ = "receivables_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    aging_buckets: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[30, 60, 90]'::jsonb"))
    auto_allocate_order: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'oldest_due_first'"))
    tds_variance_tolerance: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default=text("1.00"))
    ptp_grace_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    reminder_rules_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class Payment(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("mode", PAYMENT_MODES), name="mode"),
        CheckConstraint(_in("allocation_status", ALLOCATION_STATUSES), name="allocation_status"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("currency = 'INR' OR fx_rate IS NOT NULL", name="fx_required"),
        _fk("legal_entity_id", "legal_entities", "fk_payments_legal_entity_id_legal_entities"),
        _fk("client_id", "clients", "fk_payments_client_id_clients"),
        Index("ix_payments_client_date", "tenant_id", "client_id", "payment_date"),
        Index("ix_payments_le_date", "tenant_id", "legal_entity_id", "payment_date"),
        Index("ix_payments_reference", "tenant_id", "legal_entity_id", "reference"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    fx_rate: Mapped[Decimal | None] = mapped_column(FX, nullable=True)
    fx_source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    inr_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    receiving_bank_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    allocation_status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'unallocated'"))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reversal_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reversal_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    prior_period: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class Allocation(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "allocations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("source_kind", ("payment", "credit_note")), name="source_kind"),
        CheckConstraint(
            "(source_kind = 'payment' AND payment_id IS NOT NULL AND credit_note_id IS NULL) OR "
            "(source_kind = 'credit_note' AND credit_note_id IS NOT NULL AND payment_id IS NULL)",
            name="source_ref",
        ),
        CheckConstraint(_ONE_TARGET, name="one_target"),
        CheckConstraint("amount > 0", name="amount_positive"),
        _fk("legal_entity_id", "legal_entities", "fk_allocations_legal_entity_id_legal_entities"),
        _fk("payment_id", "payments", "fk_allocations_payment_id_payments"),
        _fk("invoice_id", "invoices", "fk_allocations_invoice_id_invoices"),
        _fk("debit_note_id", "debit_notes", "fk_allocations_debit_note_id_debit_notes"),
        Index("ix_allocations_invoice", "tenant_id", "invoice_id", "allocation_date"),
        Index("ix_allocations_debit_note", "tenant_id", "debit_note_id", "allocation_date"),
        Index("ix_allocations_payment", "tenant_id", "payment_id"),
        Index("ix_allocations_client_date", "tenant_id", "client_id", "allocation_date"),
        Index(
            "uq_allocations_credit_note_invoice",
            "tenant_id",
            "credit_note_id",
            "invoice_id",
            unique=True,
            postgresql_where=text("source_kind = 'credit_note' AND applied_from_credit = false"),
        ),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    credit_note_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    debit_note_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    allocation_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    inr_at_document: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    inr_at_payment: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    realized_fx: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    applied_from_credit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    prior_period: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class AllocationReversal(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "allocation_reversals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "allocation_id", name="uq_allocation_reversals_allocation"),
        _fk("allocation_id", "allocations", "fk_allocation_reversals_allocation_id_allocations"),
        _fk("legal_entity_id", "legal_entities", "fk_allocation_reversals_le_legal_entities"),
    )

    allocation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reversal_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    inr_at_document: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    inr_at_payment: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    realized_fx: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    prior_period: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class ClientCredit(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "client_credits"
    __table_args__ = (
        CheckConstraint(_in("kind", CREDIT_KINDS), name="kind"),
        CheckConstraint("amount <> 0", name="amount_nonzero"),
        _fk("legal_entity_id", "legal_entities", "fk_client_credits_legal_entity_id_legal_entities"),
        _fk("client_id", "clients", "fk_client_credits_client_id_clients"),
        _fk("payment_id", "payments", "fk_client_credits_payment_id_payments"),
        _fk("allocation_id", "allocations", "fk_client_credits_allocation_id_allocations"),
        Index("ix_client_credits_client", "tenant_id", "client_id", "currency", "credit_date"),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    credit_date: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    credit_note_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    allocation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    prior_period: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class InvoiceBalance(TenantMixin, Base):
    """Read cache only (rebuilt nightly, checked by reconciliation; never used for guards)."""

    __tablename__ = "invoice_balances"
    __table_args__ = (
        CheckConstraint(_in("receivable_kind", ("invoice", "debit_note")), name="kind"),
        Index("ix_invoice_balances_aging", "tenant_id", "legal_entity_id", "client_id", "due_date"),
    )

    receivable_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    receivable_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    document_total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    open_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    payment_status: Mapped[str] = mapped_column(String(16), nullable=False)
    disputed_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default=text("0"))
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    rebuilt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class BankStatementRow(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "bank_statement_rows"
    __table_args__ = (
        CheckConstraint(_in("match_kind", BANK_MATCH_KINDS), name="match_kind"),
        UniqueConstraint("tenant_id", "batch_id", "row_no", name="uq_bank_statement_rows_batch_row"),
        _fk("legal_entity_id", "legal_entities", "fk_bank_statement_rows_le_legal_entities"),
        ForeignKeyConstraint(["batch_id"], ["file_import_batches.id"], name="fk_bank_statement_rows_batch_id_batches", ondelete="RESTRICT"),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bank_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default=text("'INR'"))
    reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    narrative: Mapped[str | None] = mapped_column(String(500), nullable=True)
    match_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    matched_document_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'"))
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    soft_duplicate_of: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)


# ---------------------------------------------------------------- 217_02: TDS, write-offs, disputes
class TdsSection(UUIDPk, Timestamps, AuditedMixin, Base):
    """Hybrid: ``tenant_id`` NULL = platform seed; tenants copy and edit."""

    __tablename__ = "tds_sections"
    __table_args__ = (
        CheckConstraint(_in("kind", TDS_KINDS), name="kind"),
        Index("ix_tds_sections_tenant", "tenant_id"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    default_rate_pct: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    certificate_name: Mapped[str] = mapped_column(String(40), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class TdsDeduction(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "tds_deductions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("kind", TDS_KINDS), name="kind"),
        CheckConstraint(_in("certificate_status", CERT_STATUSES), name="certificate_status"),
        CheckConstraint(_ONE_TARGET, name="one_target"),
        CheckConstraint("amount > 0", name="amount_positive"),
        _fk("legal_entity_id", "legal_entities", "fk_tds_deductions_legal_entity_id_legal_entities"),
        _fk("payment_id", "payments", "fk_tds_deductions_payment_id_payments"),
        _fk("invoice_id", "invoices", "fk_tds_deductions_invoice_id_invoices"),
        _fk("debit_note_id", "debit_notes", "fk_tds_deductions_debit_note_id_debit_notes"),
        Index("ix_tds_deductions_invoice", "tenant_id", "invoice_id"),
        Index("ix_tds_deductions_debit_note", "tenant_id", "debit_note_id"),
        Index("ix_tds_deductions_client_quarter", "tenant_id", "client_id", "quarter"),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    debit_note_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    section_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    section_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    rate_pct: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    base_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    expected_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    inr_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    variance_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    deduction_date: Mapped[date] = mapped_column(Date, nullable=False)
    quarter: Mapped[str] = mapped_column(String(10), nullable=False)
    certificate_status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'expected'"))
    certificate_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    certificate_received_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    prior_period: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class TdsDeductionReversal(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "tds_deduction_reversals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "tds_deduction_id", name="uq_tds_deduction_reversals_deduction"),
        _fk("tds_deduction_id", "tds_deductions", "fk_tds_deduction_reversals_deduction"),
        _fk("legal_entity_id", "legal_entities", "fk_tds_deduction_reversals_le"),
    )

    tds_deduction_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reversal_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    prior_period: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class WriteOffRequest(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "write_off_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", WRITE_OFF_STATUSES), name="status"),
        CheckConstraint(_ONE_TARGET, name="one_target"),
        CheckConstraint("requested_amount > 0", name="amount_positive"),
        _fk("legal_entity_id", "legal_entities", "fk_write_off_requests_le_legal_entities"),
        _fk("invoice_id", "invoices", "fk_write_off_requests_invoice_id_invoices"),
        _fk("debit_note_id", "debit_notes", "fk_write_off_requests_debit_note_id_debit_notes"),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    debit_note_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    requested_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending_approval'"))
    status_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    write_off_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class WriteOff(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "write_offs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_ONE_TARGET, name="one_target"),
        CheckConstraint("amount > 0", name="amount_positive"),
        _fk("legal_entity_id", "legal_entities", "fk_write_offs_legal_entity_id_legal_entities"),
        _fk("request_id", "write_off_requests", "fk_write_offs_request_id_write_off_requests"),
        _fk("invoice_id", "invoices", "fk_write_offs_invoice_id_invoices"),
        _fk("debit_note_id", "debit_notes", "fk_write_offs_debit_note_id_debit_notes"),
        Index("ix_write_offs_invoice", "tenant_id", "invoice_id"),
        Index("ix_write_offs_debit_note", "tenant_id", "debit_note_id"),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    debit_note_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    write_off_date: Mapped[date] = mapped_column(Date, nullable=False)
    inr_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    residual_fx: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default=text("0"))
    prior_period: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class WriteOffReversal(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "write_off_reversals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "write_off_id", name="uq_write_off_reversals_write_off"),
        _fk("write_off_id", "write_offs", "fk_write_off_reversals_write_off_id_write_offs"),
        _fk("legal_entity_id", "legal_entities", "fk_write_off_reversals_le"),
    )

    write_off_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reversal_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    prior_period: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class Dispute(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "disputes"
    __table_args__ = (
        CheckConstraint(_in("status", DISPUTE_STATUSES), name="status"),
        CheckConstraint(_ONE_TARGET, name="one_target"),
        CheckConstraint("amount > 0", name="amount_positive"),
        _fk("legal_entity_id", "legal_entities", "fk_disputes_legal_entity_id_legal_entities"),
        _fk("invoice_id", "invoices", "fk_disputes_invoice_id_invoices"),
        _fk("debit_note_id", "debit_notes", "fk_disputes_debit_note_id_debit_notes"),
        Index("ix_disputes_client_status", "tenant_id", "client_id", "status"),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    debit_note_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    line_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'"))
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'open'"))
    resolution_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    linked_credit_note_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------- 217_03: collections, exports, reconciliation, close
class CollectionActivity(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "collection_activities"
    __pii_fields__ = ()
    __table_args__ = (
        CheckConstraint(_in("kind", ACTIVITY_KINDS), name="kind"),
        CheckConstraint("ptp_status IS NULL OR " + _in("ptp_status", PTP_STATUSES), name="ptp_status"),
        CheckConstraint("kind <> 'promise_to_pay' OR (ptp_amount IS NOT NULL AND ptp_date IS NOT NULL)", name="ptp_fields"),
        CheckConstraint("document_kind IS NULL OR " + _in("document_kind", ("invoice", "debit_note")), name="document_kind"),
        _fk("client_id", "clients", "fk_collection_activities_client_id_clients"),
        Index("ix_collection_activities_client", "tenant_id", "client_id", "occurred_at"),
        Index("ix_collection_activities_ptp", "tenant_id", "ptp_status", "ptp_date"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_kind: Mapped[str | None] = mapped_column(String(12), nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    ptp_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    ptp_currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    ptp_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ptp_status: Mapped[str | None] = mapped_column(String(8), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class ClientCollectionSettings(Timestamps, AuditedMixin, Base):
    __tablename__ = "client_collection_settings"
    __audit_entity_id__ = "client_id"
    __table_args__ = (_fk("client_id", "clients", "fk_client_collection_settings_client_id_clients", "CASCADE"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    do_not_remind: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reminder_rule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class AccountingExportTemplate(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "accounting_export_templates"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    columns: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    account_map: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class AccountingExport(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "accounting_exports"
    __audit_exclude__ = ("rows",)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", ("queued", "completed", "failed")), name="status"),
        CheckConstraint(_in("format", ("csv", "xlsx")), name="format"),
        _fk("legal_entity_id", "legal_entities", "fk_accounting_exports_le_legal_entities"),
        _fk("template_id", "accounting_export_templates", "fk_accounting_exports_template_id"),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date] = mapped_column(Date, nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    format: Mapped[str] = mapped_column(String(4), nullable=False, server_default=text("'csv'"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'queued'"))
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    flagged_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rows: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class AccountingExportItem(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "accounting_export_items"
    __table_args__ = (
        _fk("export_id", "accounting_exports", "fk_accounting_export_items_export_id", "CASCADE"),
        Index("ix_accounting_export_items_doc", "tenant_id", "doc_kind", "doc_id"),
    )

    export_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    doc_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    doc_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    previously_exported_in: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class ReconciliationRun(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", ("running", "completed", "failed")), name="status"),
    )

    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'running'"))
    findings_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class ReconciliationFinding(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "reconciliation_findings"
    __table_args__ = (
        CheckConstraint(_in("severity", ("error", "warning")), name="severity"),
        _fk("run_id", "reconciliation_runs", "fk_reconciliation_findings_run_id", "CASCADE"),
        Index("ix_reconciliation_findings_run", "tenant_id", "run_id"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    check: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class PeriodCloseEvent(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "period_close_events"
    __table_args__ = (
        CheckConstraint(_in("action", CLOSE_ACTIONS), name="action"),
        ForeignKeyConstraint(["accounting_period_id"], ["accounting_periods.id"], name="fk_period_close_events_period_id", ondelete="RESTRICT"),
        _fk("legal_entity_id", "legal_entities", "fk_period_close_events_le"),
        Index("ix_period_close_events_period", "tenant_id", "accounting_period_id", "at"),
    )

    accounting_period_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    checklist_snapshot: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    acknowledged_warnings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    request_status: Mapped[str | None] = mapped_column(String(12), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ================================================================ Epic 51 (branch p3_fin_carry): 26AS / AIS TDS reconciliation
# Migration ``20261007_315_02_tds_recon``. Runs are keyed by Legal Entity PAN + FY + quarter; deducting Clients are
# identified through ``tds_deductor_tans``. Reconciliation never writes ``tds_deductions`` / reversals (FR-254).
RECON_RUN_STATUSES = ("parsing", "ready", "failed", "superseded")
RECON_MATCH_STATUSES = ("proposed", "confirmed", "rejected", "unmatched_ack")
QUARTERS = ("Q1", "Q2", "Q3", "Q4")


class TdsDeductorTan(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "tds_deductor_tans"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "tan", name="uq_tds_deductor_tans_tenant_id_tan"),
        CheckConstraint("tan ~ '^[A-Z]{4}[0-9]{5}[A-Z]$'", name="tan_format"),
        _fk("client_id", "clients", "fk_tds_deductor_tans_client_id_clients"),
        Index("ix_tds_deductor_tans_client_id", "client_id"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tan: Mapped[str] = mapped_column(CHAR(10), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class TdsReconRun(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "tds_recon_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("fy ~ '^[0-9]{4}-[0-9]{2}$'", name="fy_format"),
        CheckConstraint(_in("quarter", QUARTERS), name="quarter"),
        CheckConstraint("statement_kind IN ('26as', 'ais')", name="statement_kind"),
        CheckConstraint("source IN ('upload', 'connector')", name="source"),
        CheckConstraint(_in("status", RECON_RUN_STATUSES), name="status"),
        _fk("legal_entity_id", "legal_entities", "fk_tds_recon_runs_legal_entity_id_legal_entities"),
        _fk("file_id", "stored_files", "fk_tds_recon_runs_file_id_stored_files"),
        Index("ix_tds_recon_runs_le_fy_quarter", "tenant_id", "legal_entity_id", "fy", "quarter"),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    fy: Mapped[str] = mapped_column(CHAR(7), nullable=False)
    quarter: Mapped[str] = mapped_column(CHAR(2), nullable=False)
    statement_kind: Mapped[str] = mapped_column(String(5), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'upload'"))
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'parsing'"))
    line_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    parse_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    assessment_year: Mapped[str | None] = mapped_column(String(9), nullable=True)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TdsStatementLine(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "tds_statement_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk("run_id", "tds_recon_runs", "fk_tds_statement_lines_run_id_tds_recon_runs"),
        Index("ix_tds_statement_lines_run_id", "run_id"),
        Index("ix_tds_statement_lines_line_hash", "tenant_id", "line_hash"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    line_ref: Mapped[str] = mapped_column(String(100), nullable=False)
    deductor_tan: Mapped[str] = mapped_column(CHAR(10), nullable=False)
    deductor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    section: Mapped[str] = mapped_column(String(10), nullable=False)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount_paid: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tds_deducted: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tds_deposited: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    booking_status: Mapped[str | None] = mapped_column(CHAR(1), nullable=True)
    line_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)


class TdsReconMatch(UUIDPk, TenantMixin, Timestamps, Base):
    """A proposed / decided pairing of a statement line with a recorded TDS Deduction. Confirming is human-only
    (FR-257, ``tds_recon_match: confirmed``). ``active`` turns false when the run is superseded."""

    __tablename__ = "tds_recon_matches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", RECON_MATCH_STATUSES), name="status"),
        CheckConstraint("status = 'unmatched_ack' OR tds_deduction_id IS NOT NULL", name="target"),
        CheckConstraint("score BETWEEN 0 AND 100", name="score"),
        _fk("statement_line_id", "tds_statement_lines", "fk_tds_recon_matches_statement_line_id"),
        _fk("tds_deduction_id", "tds_deductions", "fk_tds_recon_matches_tds_deduction_id"),
        Index("ix_tds_recon_matches_statement_line_id", "statement_line_id"),
        Index("ix_tds_recon_matches_tds_deduction_id", "tds_deduction_id"),
        Index(
            "uq_tds_recon_matches_open_line",
            "statement_line_id",
            unique=True,
            postgresql_where=text("active AND status IN ('proposed', 'confirmed', 'unmatched_ack')"),
        ),
        Index(
            "uq_tds_recon_matches_confirmed_deduction",
            "tds_deduction_id",
            unique=True,
            postgresql_where=text("active AND status = 'confirmed'"),
        ),
    )

    statement_line_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tds_deduction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(15), nullable=False, server_default=text("'proposed'"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    score: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_actor: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    carried_from_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
