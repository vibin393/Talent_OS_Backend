"""File import batches (AD-23, FR-185): each previewed / applied file with its SHA-256, so the same file is never
applied twice (partial UNIQUE on applied rows). Migration 20261002_200_06; internal-only org RLS.

The spec names this table ``import_batches``, but Phase 1 (Epic 3, candidate bulk import) already owns a table
of that name, so the foundation table is ``file_import_batches`` (Epic 13 deviation, recorded in the spec).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

IMPORT_STATUSES = ("previewed", "applied", "failed", "discarded")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class FileImportBatch(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "file_import_batches"
    __audit_exclude__ = ("summary",)
    __table_args__ = (
        CheckConstraint(_in("status", IMPORT_STATUSES), name="status"),
        CheckConstraint("row_count >= 0 AND error_count >= 0", name="counts"),
        ForeignKeyConstraint(
            ["tenant_id", "file_id"],
            ["stored_files.tenant_id", "stored_files.id"],
            name="fk_file_import_batches_file_id_stored_files",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_file_import_batches_applied",
            "tenant_id",
            "kind",
            "sha256",
            unique=True,
            # A forced re-apply (audited) is applied too but does not count against the duplicate guard.
            postgresql_where=text("status = 'applied' AND (summary ->> 'forced_reapply_of') IS NULL"),
        ),
    )

    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'previewed'"))
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False, server_default=text("'{}'::jsonb"))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
