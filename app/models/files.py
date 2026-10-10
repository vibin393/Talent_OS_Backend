"""Stored files and generic attachments (AD-13). Tenant-owned, RLS (migration 20261001_33)."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

SCAN_STATUSES = ("clean", "infected", "skipped_dev", "error")
ATTACHMENT_CATEGORIES = ("certificate", "id_proof", "offer_letter", "relieving_letter", "payslip", "other")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class StoredFile(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    """One uploaded, scanned file. ``owner_entity_*`` names the record whose access rules govern download."""

    __tablename__ = "stored_files"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("storage_key"),
        CheckConstraint(_in("scan_status", SCAN_STATUSES), name="scan_status"),
        Index("ix_stored_files_owner", "owner_entity_type", "owner_entity_id"),
    )
    # The original file name may contain a person's name: never copied into the audit log.
    __audit_exclude__ = ("original_name", "storage_key")

    storage_key: Mapped[str] = mapped_column(String(200), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    scan_status: Mapped[str] = mapped_column(String(20), nullable=False)
    owner_entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    owner_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Epic 12 (erasure): the object was deleted from storage; the row stays as a tombstone.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Attachment(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "attachments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "file_id"],
            ["stored_files.tenant_id", "stored_files.id"],
            name="fk_attachments_file_id_stored_files",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_in("category", ATTACHMENT_CATEGORIES), name="category"),
        Index("ix_attachments_entity", "entity_type", "entity_id"),
    )

    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Epic 22 (209_03): the ``documents`` row this attachment was migrated to (the table is read-only now).
    migrated_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
