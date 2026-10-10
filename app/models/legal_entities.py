"""Legal Entities, GST registrations, numbering series, accounting periods and FX rates (Epic 13, AD-17,
AD-18, AD-22). Tenant-owned with forced RLS (migration 20261002_200_02).

Domain ``legal_entities`` solely owns these tables. Bank account numbers are ``EncryptedStr`` (AD-35) with
``_last4`` / ``_bidx`` companions and are never audited in plaintext.
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
    PrimaryKeyConstraint,
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

from app.core.encrypted import EncryptedStr, Sealed
from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

ENTITY_STATUSES = ("active", "inactive")
REGISTRATION_STATUSES = ("active", "inactive")
DOC_TYPES = ("invoice", "credit_note", "debit_note")
FY_FORMATS = ("YY-YY", "YYYY-YY", "YY", "none")
SEPARATORS = ("/", "-", "")
SERIES_STATUSES = ("active", "inactive")
PERIOD_STATUSES = ("open", "closed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class LegalEntity(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "legal_entities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", ENTITY_STATUSES), name="status"),
        CheckConstraint("pan IS NULL OR pan ~ '^[A-Z]{5}[0-9]{4}[A-Z]$'", name="pan_format"),
        _tenant_fk("logo_file_id", "stored_files", "fk_legal_entities_logo_file_id_stored_files"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str] = mapped_column(String(300), nullable=False)
    pan: Mapped[str | None] = mapped_column(CHAR(10), nullable=True)
    cin: Mapped[str | None] = mapped_column(String(21), nullable=True)
    address: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False, server_default=text("'{}'::jsonb"))
    state_code: Mapped[str | None] = mapped_column(
        CHAR(2), ForeignKey("gst_states.code", ondelete="RESTRICT"), nullable=True
    )
    country: Mapped[str] = mapped_column(CHAR(2), nullable=False, server_default=text("'IN'"))
    default_currency: Mapped[str] = mapped_column(
        CHAR(3), ForeignKey("currencies.code", ondelete="RESTRICT"), nullable=False
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'Asia/Kolkata'"))
    logo_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    signatory_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    signatory_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))
    is_backfilled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # Epic 14 (FR-76): why / when the entity was deactivated.
    deactivation_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LegalEntityBankAccount(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "legal_entity_bank_accounts"
    __audit_exclude__ = ("account_number", "account_number_bidx")
    __table_args__ = (
        _tenant_fk(
            "legal_entity_id",
            "legal_entities",
            "fk_legal_entity_bank_accounts_legal_entity_id_legal_entities",
            "CASCADE",
        ),
        # Epic 14 (migration 20261003_201_01): one default per (entity, currency) among non-archived accounts.
        Index(
            "uq_legal_entity_bank_accounts_default",
            "legal_entity_id",
            text("coalesce(currency, '')"),
            unique=True,
            postgresql_where=text("is_default AND archived_at IS NULL"),
        ),
        Index("ix_legal_entity_bank_accounts_legal_entity_id", "legal_entity_id"),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(200), nullable=False)
    account_number: Mapped[Sealed] = mapped_column(EncryptedStr(), nullable=False)
    account_number_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    account_number_bidx: Mapped[str] = mapped_column(String(32), nullable=False)
    ifsc: Mapped[str | None] = mapped_column(String(11), nullable=True)
    swift: Mapped[str | None] = mapped_column(String(11), nullable=True)
    # NULL = usable for any document currency.
    currency: Mapped[str | None] = mapped_column(
        CHAR(3), ForeignKey("currencies.code", ondelete="RESTRICT"), nullable=True
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # Epic 14 (FR-76): admin details and soft archive.
    branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    account_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GstRegistration(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "gst_registrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "gstin"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("state_code = substr(gstin, 1, 2)", name="state_matches_gstin"),
        CheckConstraint(_in("status", REGISTRATION_STATUSES), name="status"),
        _tenant_fk(
            "legal_entity_id", "legal_entities", "fk_gst_registrations_legal_entity_id_legal_entities"
        ),
        Index(
            "uq_gst_registrations_principal",
            "legal_entity_id",
            unique=True,
            postgresql_where=text("is_principal"),
        ),
        Index("ix_gst_registrations_legal_entity_id", "legal_entity_id"),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    gstin: Mapped[str] = mapped_column(CHAR(15), nullable=False)
    state_code: Mapped[str] = mapped_column(
        CHAR(2), ForeignKey("gst_states.code", ondelete="RESTRICT"), nullable=False
    )
    address: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False, server_default=text("'{}'::jsonb"))
    is_principal: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    einvoice_applicable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    irp_reporting_limit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))


class GstRegistrationLut(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "gst_registration_luts"
    __table_args__ = (
        UniqueConstraint("gst_registration_id", "fiscal_year"),
        CheckConstraint("fiscal_year ~ '^[0-9]{2}-[0-9]{2}$'", name="fiscal_year"),
        CheckConstraint("valid_to >= valid_from", name="validity"),
        _tenant_fk(
            "gst_registration_id",
            "gst_registrations",
            "fk_gst_registration_luts_gst_registration_id_gst_registrations",
            "CASCADE",
        ),
    )

    gst_registration_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    fiscal_year: Mapped[str] = mapped_column(CHAR(5), nullable=False)
    lut_number: Mapped[str] = mapped_column(String(50), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)


class DocumentSeries(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "document_series"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("doc_type", DOC_TYPES), name="doc_type"),
        CheckConstraint(_in("fy_format", FY_FORMATS), name="fy_format"),
        CheckConstraint(_in("separator", SEPARATORS), name="separator"),
        CheckConstraint(_in("status", SERIES_STATUSES), name="status"),
        CheckConstraint("pad BETWEEN 1 AND 10", name="pad"),
        CheckConstraint("prefix ~ '^[A-Za-z0-9/-]*$'", name="prefix_chars"),
        _tenant_fk("legal_entity_id", "legal_entities", "fk_document_series_legal_entity_id_legal_entities"),
        _tenant_fk(
            "gst_registration_id",
            "gst_registrations",
            "fk_document_series_gst_registration_id_gst_registrations",
        ),
        Index(
            "uq_document_series_active",
            "tenant_id",
            "gst_registration_id",
            "doc_type",
            unique=True,
            postgresql_where=text("status = 'active' AND NOT is_legacy"),
        ),
        Index("ix_document_series_legal_entity_id", "legal_entity_id"),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    gst_registration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    doc_type: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    prefix: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("''"))
    fy_format: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'YY-YY'"))
    separator: Mapped[str] = mapped_column(String(1), nullable=False, server_default=text("'/'"))
    pad: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("4"))
    is_legacy: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    allow_backdated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))


class DocumentSeriesCounter(TenantMixin, Base):
    """Gap-free counter per series and financial year (AD-18); advanced only by ``numbering.assign``."""

    __tablename__ = "document_series_counters"
    __table_args__ = (
        PrimaryKeyConstraint("series_id", "fiscal_year"),
        _tenant_fk(
            "series_id", "document_series", "fk_document_series_counters_series_id_document_series", "CASCADE"
        ),
        CheckConstraint("last_value >= 0", name="last_value"),
    )

    series_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    fiscal_year: Mapped[str] = mapped_column(CHAR(5), nullable=False)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_doc_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class AccountingPeriod(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "accounting_periods"
    __table_args__ = (
        UniqueConstraint("legal_entity_id", "year", "month"),
        CheckConstraint("month BETWEEN 1 AND 12", name="month"),
        CheckConstraint(_in("status", PERIOD_STATUSES), name="status"),
        _tenant_fk(
            "legal_entity_id", "legal_entities", "fk_accounting_periods_legal_entity_id_legal_entities"
        ),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'open'"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reopened_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class FxRate(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "fx_rates"
    __table_args__ = (
        # Epic 51 (315_03, coordinator decision 2): a manual and a feed rate may coexist for one date.
        UniqueConstraint(
            "tenant_id", "from_currency", "to_currency", "rate_date", "origin", name="uq_fx_rates_pair_date_origin"
        ),
        CheckConstraint("origin IN ('manual', 'feed')", name="origin"),
        CheckConstraint("rate > 0", name="rate_positive"),
        CheckConstraint("source <> ''", name="source_required"),
        CheckConstraint("from_currency <> to_currency", name="distinct_currencies"),
    )

    from_currency: Mapped[str] = mapped_column(
        CHAR(3), ForeignKey("currencies.code", ondelete="RESTRICT"), nullable=False
    )
    to_currency: Mapped[str] = mapped_column(
        CHAR(3), ForeignKey("currencies.code", ondelete="RESTRICT"), nullable=False
    )
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    origin: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'manual'"))


# ================================================================ Epic 51 (branch p3_fin_carry): live FX feed
# Migration ``20261007_315_03_fx_feed``. Feed rows land in ``fx_rates`` with ``origin='feed'``; manual rows are never
# touched by the feed.
FX_PRECEDENCES = ("manual_first", "feed_first")
FX_FEED_RUN_STATUSES = ("ok", "partial", "failed")


class FxFeedSettings(Timestamps, AuditedMixin, Base):
    __tablename__ = "fx_feed_settings"
    __table_args__ = (
        CheckConstraint("precedence IN ('manual_first', 'feed_first')", name="precedence"),
        CheckConstraint("max_stale_days BETWEEN 1 AND 7", name="max_stale_days"),
        CheckConstraint("fetch_hour_ist BETWEEN 0 AND 23", name="fetch_hour_ist"),
    )
    __audit_entity_id__ = "tenant_id"
    __audit_exclude__ = ("consecutive_failures", "last_alert_on")

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    pairs: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{USD/INR,EUR/INR,GBP/INR}'::text[]")
    )
    precedence: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'manual_first'"))
    max_stale_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("3"))
    fetch_hour_ist: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("10"))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_alert_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class FxFeedRun(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "fx_feed_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "on_date", "provider_key", name="uq_fx_feed_runs_date_provider"),
        CheckConstraint("status IN ('ok', 'partial', 'failed')", name="status"),
        Index("ix_fx_feed_runs_tenant_on_date", "tenant_id", "on_date"),
    )

    on_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(40), nullable=False)
    fetched: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    skipped_manual: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
