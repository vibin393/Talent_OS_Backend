"""Posting ledgers (AD-23): approved work is billed and paid exactly once, by quantity.

Written only by ``app/services/postings.py``. Rows are immutable except ``state`` / ``posted_at`` /
``released_at`` / ``release_reason``; the state guard allows ``reserved -> posted|released`` and
``posted -> released`` only and refuses DELETE. The partial UNIQUE ``(tenant_id, source_kind, source_id)
WHERE state <> 'released'`` makes a source item active in at most one posting per ledger. Internal-only org RLS
(migration 20261002_200_06).
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CHAR, CheckConstraint, DateTime, Index, Numeric, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TenantMixin, UUIDPk

POSTING_STATES = ("reserved", "posted", "released")
BILLING_TARGET_KINDS = ("invoice_line", "credit_note_line", "debit_note_line")
PAYABLE_TARGET_KINDS = ("payroll_row", "vendor_statement_line")
PAYEE_KINDS = ("worker", "vendor")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class _PostingColumns(UUIDPk, TenantMixin):
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    state: Mapped[str] = mapped_column(String(10), nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class BillingPosting(_PostingColumns, Base):
    __tablename__ = "billing_postings"
    __table_args__ = (
        CheckConstraint(_in("state", POSTING_STATES), name="state"),
        CheckConstraint(_in("target_kind", BILLING_TARGET_KINDS), name="target_kind"),
        Index(
            "uq_billing_postings_active",
            "tenant_id",
            "source_kind",
            "source_id",
            unique=True,
            postgresql_where=text("state <> 'released'"),
        ),
        Index("ix_billing_postings_document_id", "document_id"),
        Index("ix_billing_postings_target", "target_kind", "target_id"),
        Index("ix_billing_postings_source", "source_kind", "source_id"),
    )


class PayablePosting(_PostingColumns, Base):
    __tablename__ = "payable_postings"
    __table_args__ = (
        CheckConstraint(_in("state", POSTING_STATES), name="state"),
        CheckConstraint(_in("target_kind", PAYABLE_TARGET_KINDS), name="target_kind"),
        CheckConstraint(_in("payee_kind", PAYEE_KINDS), name="payee_kind"),
        Index(
            "uq_payable_postings_active",
            "tenant_id",
            "source_kind",
            "source_id",
            unique=True,
            postgresql_where=text("state <> 'released'"),
        ),
        Index("ix_payable_postings_document_id", "document_id"),
        Index("ix_payable_postings_target", "target_kind", "target_id"),
        Index("ix_payable_postings_source", "source_kind", "source_id"),
    )

    payee_kind: Mapped[str] = mapped_column(String(10), nullable=False)
