# ruff: file-ignore[line-too-long, invalid-formatter-suppression-comment]
"""Public API tables (Epic 67, spec A2; migration ``20261010_501_01_public_api``). Owner: ``app.services.public_api``.

* ``public_import_jobs``: the public wrapper around a P1 bulk import batch (``POST /public/v1/imports``).
* ``api_deprecation_notices``: one row per (Installation | API key, month, deprecated route) from the monthly report.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TenantMixin, Timestamps, UUIDPk

IMPORT_RESOURCES = ("candidates", "applications")
IMPORT_FORMATS = ("csv", "jsonl")
IMPORT_STATUSES = ("queued", "validating", "running", "completed", "failed", "cancelled")


class PublicImportJob(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "public_import_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("resource IN ('candidates', 'applications')", name="resource"),
        CheckConstraint("format IN ('csv', 'jsonl')", name="format"),
        CheckConstraint("status IN ('queued', 'validating', 'running', 'completed', 'failed', 'cancelled')", name="status"),
        CheckConstraint("installation_id IS NOT NULL OR api_key_id IS NOT NULL", name="owner"),
    )

    installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    acting_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    resource: Mapped[str] = mapped_column(String(20), nullable=False)
    format: Mapped[str] = mapped_column(String(10), nullable=False)
    file_key: Mapped[str] = mapped_column(String(300), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'queued'"))
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    error_report_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiDeprecationNotice(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "api_deprecation_notices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint(
            "tenant_id", "installation_id", "api_key_id", "month", "route_template",
            name="uq_api_deprecation_notices_key", postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("installation_id IS NOT NULL OR api_key_id IS NOT NULL", name="owner"),
    )

    installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    month: Mapped[date] = mapped_column(Date, nullable=False)
    route_template: Mapped[str] = mapped_column(String(300), nullable=False)
    call_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sunset: Mapped[date] = mapped_column(Date, nullable=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["IMPORT_FORMATS", "IMPORT_RESOURCES", "IMPORT_STATUSES", "ApiDeprecationNotice", "PublicImportJob"]
