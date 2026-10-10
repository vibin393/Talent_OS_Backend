"""Go-live import rows (Epic 35 A8, FR-185; migration 20261005_222_02).

A go-live import is a foundation ``file_import_batches`` row (``kind = 'golive.<kind>'``) plus one
``golive_import_rows`` row per CSV data row with its validation errors and apply result. Raw row values are never
stored here (bank / identity columns are encrypted only by their owner on apply, AD-35): apply re-reads the
scanned file.
"""

import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

GOLIVE_KINDS = ("workers", "placements", "opening_invoices", "leave_balances")
GOLIVE_ROW_STATUSES = ("valid", "invalid", "applied", "failed", "skipped")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class GoLiveImportRow(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "golive_import_rows"
    __audit_exclude__ = ("errors",)
    __table_args__ = (
        ForeignKeyConstraint(
            ["batch_id"],
            ["file_import_batches.id"],
            name="fk_golive_import_rows_batch_id_file_import_batches",
            ondelete="CASCADE",
        ),
        UniqueConstraint("batch_id", "row_no"),
        CheckConstraint(_in("status", GOLIVE_ROW_STATUSES), name="status"),
        Index("ix_golive_import_rows_batch_status", "batch_id", "status"),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    result_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
