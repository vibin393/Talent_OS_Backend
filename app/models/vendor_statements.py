"""Vendor Payable Statements (Epic 17 A8, FR-98; payroll sub-domain, AD-23 / AD-21).

Written only by ``app/services/payroll/vendor_statements.py``. Both tables carry org RLS ``vendor_col`` (a
vendor session sees its own statements) and ``enable_immutability`` (mutable while ``draft``; lines inherit
the parent's state), so a finalized statement is never edited (409 ``document_immutable``).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TenantMixin, Timestamps, UUIDPk

STATEMENT_STATUSES = ("draft", "finalized", "superseded")
LINE_KINDS = ("time", "expense", "adjustment")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tfk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class VendorPayableStatement(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "vendor_payable_statements"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("status", STATEMENT_STATUSES), name="status"),
        CheckConstraint("period_end >= period_start", name="period"),
        _tfk("vendor_id", "vendors", "fk_vendor_payable_statements_vendor_id_vendors"),
        _tfk("legal_entity_id", "legal_entities", "fk_vendor_payable_statements_legal_entity_id"),
        Index("ix_vendor_payable_statements_vendor_id", "vendor_id"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    pay_period_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )  # soft ref (Epic 28)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'draft'"))
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default=text("'INR'"))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"))
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    built_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    finalized_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    csv_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class VendorStatementLine(UUIDPk, TenantMixin, Base):
    __tablename__ = "vendor_statement_lines"
    __table_args__ = (
        CheckConstraint(_in("line_kind", LINE_KINDS), name="line_kind"),
        _tfk(
            "statement_id", "vendor_payable_statements", "fk_vendor_statement_lines_statement_id", "CASCADE"
        ),
        Index("ix_vendor_statement_lines_statement_id", "statement_id"),
    )

    statement_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    line_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    worker_name_snapshot: Mapped[str | None] = mapped_column(String(200), nullable=True)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    time_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    vendor_rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False, server_default=text("1"))
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    adjusts_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
