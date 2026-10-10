# ruff: noqa: E501
"""Ledger sync (Epic 71, stories 71.4-71.6, FR-368..FR-370; migration ``<date>_505_02_ledger_sync``). Owner:
``app.services.ledger_sync``.

* ``ledger_mappings`` -- per Installation x Legal Entity: revenue / GST / TDS / round-off / customer / item / bank /
  discount -> external ledger id or name (audited).
* ``ledger_sync_records`` -- one row per (Installation, document): push state per document version (not audited).
* ``ledger_runs`` -- push / Tally file / pull / status / masters runs (audited); the Tally XML is a stored file.
* ``payment_suggestions`` -- payments pulled from the ledger, waiting for a Finance User to record them (★ confirm).
* ``ledger_settings`` -- one row per tenant (audited).
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
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

MAPPING_KINDS = ("revenue", "gst_output", "tds_receivable", "round_off", "customer", "item", "bank_account", "discount")
DOCUMENT_KINDS = ("invoice", "credit_note", "debit_note", "payment")
SYNC_STATUSES = ("pending", "synced", "failed", "skipped", "out_of_sync", "blocked_period")
RUN_KINDS = ("push", "tally_file", "pull_payments", "status", "masters")
RUN_STATUSES = ("queued", "running", "completed", "failed")
SUGGESTION_STATUSES = ("pending", "confirmed", "rejected", "stale")
MATCH_NOTES = ("invoice_not_found", "currency_mismatch", "over_payment")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


class LedgerMapping(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "ledger_mappings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "installation_id", "legal_entity_id", "kind", "key", name="uq_ledger_mappings_key"),
        _fk("installation_id", "app_installations", "fk_ledger_mappings_installation", "CASCADE"),
        _fk("legal_entity_id", "legal_entities", "fk_ledger_mappings_legal_entity"),
        CheckConstraint(_in("kind", MAPPING_KINDS), name="kind"),
    )

    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    external_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    external_label: Mapped[str | None] = mapped_column(String(200), nullable=True)


class LedgerSyncRecord(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "ledger_sync_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "installation_id", "document_kind", "document_id", name="uq_ledger_sync_records_doc"),
        _fk("installation_id", "app_installations", "fk_ledger_sync_records_installation", "CASCADE"),
        CheckConstraint(_in("document_kind", DOCUMENT_KINDS), name="document_kind"),
        CheckConstraint(_in("status", SYNC_STATUSES), name="status"),
        Index("ix_ledger_sync_records_status_date", "status", "doc_date"),
        Index("ix_ledger_sync_records_entity_date", "legal_entity_id", "doc_date"),
    )

    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    synced_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(14), nullable=False, server_default=text("'pending'"))
    external_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    external_number: Mapped[str | None] = mapped_column(String(60), nullable=True)
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    skipped_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tally_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    doc_date: Mapped[date] = mapped_column(Date, nullable=False)


class LedgerRun(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "ledger_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk("installation_id", "app_installations", "fk_ledger_runs_installation", "CASCADE"),
        _fk("legal_entity_id", "legal_entities", "fk_ledger_runs_legal_entity"),
        CheckConstraint(_in("kind", RUN_KINDS), name="kind"),
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        Index("ix_ledger_runs_installation_created", "installation_id", text("created_at DESC")),
    )

    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    from_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    to_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    include_masters: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'queued'"))
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaymentSuggestion(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "payment_suggestions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "installation_id", "external_ref", name="uq_payment_suggestions_ref"),
        _fk("installation_id", "app_installations", "fk_payment_suggestions_installation", "CASCADE"),
        CheckConstraint(_in("status", SUGGESTION_STATUSES), name="status"),
        CheckConstraint(f"match_note IS NULL OR {_in('match_note', MATCH_NOTES)}", name="match_note"),
        CheckConstraint("amount > 0", name="amount"),
        Index("ix_payment_suggestions_status_paid_on", "status", "paid_on"),
    )

    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    external_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    invoice_number: Mapped[str] = mapped_column(String(60), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    paid_on: Mapped[date] = mapped_column(Date, nullable=False)
    tds_amount: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'pending'"))
    payment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    match_note: Mapped[str | None] = mapped_column(String(200), nullable=True)


class LedgerSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "ledger_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", name="uq_ledger_settings_tenant_id"),
        CheckConstraint("pull_payments_every_minutes >= 30", name="pull_every"),
        CheckConstraint("suggestion_stale_days BETWEEN 1 AND 365", name="stale_days"),
    )

    auto_push_on_issue: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    push_payments: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    pull_payments_every_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("60"))
    suggestion_stale_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))
