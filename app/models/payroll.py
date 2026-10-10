"""Payroll Export (Epic 28, FR-140..FR-142; AD-20 / AD-21 / AD-22 / AD-23).

Written only by ``app/services/payroll/*`` (export part; vendor statements live in ``vendor_statements.py``).

* ``payroll_settings`` (one row per tenant), ``pay_calendars`` (one per Legal Entity), ``pay_periods``
  (EXCLUDE gist: one period per Legal Entity covers any date).
* ``payroll_export_versions`` / ``payroll_export_rows`` / ``payroll_export_items``: previews are mutable
  (``building|generated|superseded``); final / supplementary versions are immutable at the database once
  ``finalized`` (AD-21), except the provider columns and ``finalized -> processed``.
* ``payroll_export_mappings``: Tenant column-mapping templates for CSV / XLSX files.

No statutory computation (PRD OQ2): PF / ESI / PT / TDS columns exist only in the file mapping and are always
empty strings; nothing here stores them.
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
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TenantMixin, Timestamps, UUIDPk

LATE_ITEM_MAPPINGS = ("item_date", "approval_date")
WORKER_TYPES = ("employee", "contractor")
PERIOD_STATUSES = ("open", "locked")
VERSION_KINDS = ("preview", "final", "supplementary")
VERSION_STATUSES = ("building", "generated", "finalized", "processed", "superseded")
MUTABLE_VERSION_STATUSES = ("building", "generated", "superseded")
PROVIDER_STATUSES = ("not_sent", "sent", "processed", "failed")
FILE_FORMATS = ("csv", "xlsx")
HOUR_CATEGORIES = ("regular", "ot", "double_time", "holiday", "leave_paid", "leave_unpaid")
EARNING_CATEGORIES = ("regular", "ot", "double_time", "holiday", "leave_paid", "base")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tfk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _hours() -> Any:
    return mapped_column(Numeric(8, 2), nullable=False, server_default=text("0"))


def _money() -> Any:
    return mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"))


class PayrollSettings(TenantMixin, Timestamps, Base):
    __tablename__ = "payroll_settings"
    __table_args__ = (
        CheckConstraint(_in("late_item_mapping", LATE_ITEM_MAPPINGS), name="late_item_mapping"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    late_item_mapping: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'item_date'")
    )
    include_worker_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{employee,contractor}'::text[]")
    )


class PayCalendar(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "pay_calendars"
    __table_args__ = (
        UniqueConstraint("tenant_id", "legal_entity_id"),
        CheckConstraint("frequency = 'monthly'", name="frequency"),
        CheckConstraint("cutoff_day IS NULL OR cutoff_day BETWEEN 1 AND 28", name="cutoff_day"),
        _tfk("legal_entity_id", "legal_entities", "fk_pay_calendars_legal_entity_id"),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    frequency: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'monthly'"))
    cutoff_day: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # NULL = last day of month
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class PayPeriod(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "pay_periods"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", PERIOD_STATUSES), name="status"),
        CheckConstraint("period_end >= period_start", name="period"),
        _tfk("legal_entity_id", "legal_entities", "fk_pay_periods_legal_entity_id"),
        Index("ix_pay_periods_le_start", "legal_entity_id", "period_start"),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    human_id: Mapped[str] = mapped_column(String(40), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'open'"))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reopened_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_reopen_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class PayrollExportVersion(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "payroll_export_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "pay_period_id", "seq"),
        CheckConstraint(_in("kind", VERSION_KINDS), name="kind"),
        CheckConstraint(_in("status", VERSION_STATUSES), name="status"),
        CheckConstraint(_in("provider_status", PROVIDER_STATUSES), name="provider_status"),
        _tfk("pay_period_id", "pay_periods", "fk_payroll_export_versions_pay_period_id"),
        _tfk("legal_entity_id", "legal_entities", "fk_payroll_export_versions_legal_entity_id"),
        Index(
            "uq_payroll_export_versions_one_final",
            "pay_period_id",
            unique=True,
            postgresql_where=text("kind = 'final'"),
        ),
        Index("ix_payroll_export_versions_pay_period_id", "pay_period_id"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    pay_period_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(14), nullable=False)
    supplementary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'building'"))
    based_on_preview_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    exclude_exceptions: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    generated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalized_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    exceptions: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    totals: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    provider_status: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'not_sent'")
    )
    provider_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class PayrollExportRow(UUIDPk, TenantMixin, Base):
    __tablename__ = "payroll_export_rows"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("version_id", "candidate_id", "currency"),
        _tfk("version_id", "payroll_export_versions", "fk_payroll_export_rows_version_id", "CASCADE"),
        Index("ix_payroll_export_rows_candidate_id", "candidate_id"),
    )

    version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    worker_human_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    placement_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid(as_uuid=True)), nullable=False)
    worker_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pan_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    bank_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    ifsc: Mapped[str | None] = mapped_column(String(11), nullable=True)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    hours_regular: Mapped[Decimal] = _hours()
    hours_ot: Mapped[Decimal] = _hours()
    hours_double_time: Mapped[Decimal] = _hours()
    hours_holiday: Mapped[Decimal] = _hours()
    hours_leave_paid: Mapped[Decimal] = _hours()
    hours_leave_unpaid: Mapped[Decimal] = _hours()
    days_leave_paid: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, server_default=text("0"))
    days_leave_unpaid: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, server_default=text("0")
    )
    earnings_regular: Mapped[Decimal] = _money()
    earnings_ot: Mapped[Decimal] = _money()
    earnings_double_time: Mapped[Decimal] = _money()
    earnings_holiday: Mapped[Decimal] = _money()
    earnings_leave_paid: Mapped[Decimal] = _money()
    earnings_base: Mapped[Decimal] = _money()
    pay_rates: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    reimbursements_total: Mapped[Decimal] = _money()
    adjustments_total: Mapped[Decimal] = _money()
    rate_adjustments_total: Mapped[Decimal] = _money()
    gross_earnings_inputs: Mapped[Decimal] = _money()


class PayrollExportItem(UUIDPk, TenantMixin, Base):
    __tablename__ = "payroll_export_items"
    __table_args__ = (
        _tfk("version_id", "payroll_export_versions", "fk_payroll_export_items_version_id", "CASCADE"),
        _tfk("row_id", "payroll_export_rows", "fk_payroll_export_items_row_id", "CASCADE"),
        Index("ix_payroll_export_items_version_id", "version_id"),
        Index("ix_payroll_export_items_row_id", "row_id"),
        Index("ix_payroll_export_items_source", "source_kind", "source_id"),
    )

    version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    row_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )  # NULL: monthly base
    placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    rate_segment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    bucket: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    item_date: Mapped[date] = mapped_column(Date, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    multiplier: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)


class PayrollExportMapping(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "payroll_export_mappings"
    __table_args__ = (
        CheckConstraint(_in("format", FILE_FORMATS), name="format"),
        _tfk("legal_entity_id", "legal_entities", "fk_payroll_export_mappings_legal_entity_id"),
        Index(
            "uq_payroll_export_mappings_default",
            "tenant_id",
            text("COALESCE(legal_entity_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    format: Mapped[str] = mapped_column(String(4), nullable=False, server_default=text("'csv'"))
    columns: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
