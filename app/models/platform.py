"""Platform operations tables (Epic 12, FR-69..FR-72; migration 20261001_123).

* ``support_access_grants`` -- tenant-owned (RLS): the tenant approves/denies/revokes; operator-side access
  goes through ``tenant_session(tenant_id)``.
* Platform tables (``app.core.tenancy.PLATFORM_TABLES``, no RLS, read through ``platform_service`` with
  explicit tenant filters): ``sub_processors``, ``platform_notices``, ``platform_notice_acks``,
  ``incidents``, ``incident_notifications``, ``tenant_exports``, ``tenant_deletion_certificates``.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

TENANT_STATUSES = ("active", "suspended", "offboarding", "deleted")
SUPPORT_GRANT_STATUSES = ("pending", "approved", "denied", "revoked", "expired", "cancelled")
SUB_PROCESSOR_STATUSES = ("planned", "active", "retired")
NOTICE_AUDIENCES = ("admins", "all_users")
NOTICE_KINDS = ("incident", "sub_processor", "maintenance")
SEVERITIES = ("info", "low", "medium", "high", "critical")
INCIDENT_STATUSES = ("draft", "notified", "closed")
EXPORT_KINDS = ("offboarding", "admin")
EXPORT_STATUSES = ("queued", "running", "ready", "failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class SupportAccessGrant(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "support_access_grants"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", SUPPORT_GRANT_STATUSES), name="status"),
        CheckConstraint("requested_hours BETWEEN 1 AND 24", name="hours"),
        CheckConstraint("char_length(reason) >= 20", name="reason_length"),
        Index("ix_support_access_grants_operator_id", "operator_id"),
    )

    operator_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("platform_operators.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    ticket_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    requested_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SupportGrantLookup(Base):
    """Platform table (no PII): grant id -> tenant / operator, so operator-side listing and actions open only
    the tenant sessions they need (the grants themselves are RLS-protected)."""

    __tablename__ = "support_grant_lookups"

    grant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    operator_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SubProcessor(UUIDPk, Timestamps, Base):
    __tablename__ = "sub_processors"
    __table_args__ = (CheckConstraint(_in("status", SUB_PROCESSOR_STATUSES), name="status"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    data_categories: Mapped[list[str]] = mapped_column(
        ARRAY(String(100)), nullable=False, server_default=text("'{}'::varchar[]")
    )
    processing_location: Mapped[str] = mapped_column(String(200), nullable=False)
    country: Mapped[str] = mapped_column(String(100), nullable=False)
    website: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'planned'"))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notice_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    emergency_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class PlatformNotice(UUIDPk, Base):
    __tablename__ = "platform_notices"
    __table_args__ = (
        CheckConstraint(_in("audience", NOTICE_AUDIENCES), name="audience"),
        CheckConstraint(_in("kind", NOTICE_KINDS), name="kind"),
        CheckConstraint(_in("severity", SEVERITIES), name="severity"),
        Index("ix_platform_notices_tenant_id", "tenant_id"),
    )

    # NULL = every tenant.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True
    )
    audience: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'info'"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlatformNoticeAck(Base):
    __tablename__ = "platform_notice_acks"

    notice_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("platform_notices.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    acked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Incident(UUIDPk, Timestamps, Base):
    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(_in("status", INCIDENT_STATUSES), name="status"),
        CheckConstraint(_in("severity", SEVERITIES), name="severity"),
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    # Template variables (nature, data categories, mitigation, contact).
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class IncidentNotification(UUIDPk, Base):
    __tablename__ = "incident_notifications"
    __table_args__ = (UniqueConstraint("incident_id", "tenant_id"),)

    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    notice_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    recipient_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    late: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class TenantExport(UUIDPk, Timestamps, Base):
    __tablename__ = "tenant_exports"
    __table_args__ = (
        CheckConstraint(_in("kind", EXPORT_KINDS), name="kind"),
        CheckConstraint(_in("status", EXPORT_STATUSES), name="status"),
        Index("ix_tenant_exports_tenant_id", "tenant_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'queued'"))
    storage_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class TenantDeletionCertificate(UUIDPk, Base):
    __tablename__ = "tenant_deletion_certificates"

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    tenant_name: Mapped[str] = mapped_column(String(200), nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    files_deleted: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


# ================================================================ Phase 2 Epic 33 (FR-174, AD-38)
CUSTOM_DOMAIN_STATUSES = ("pending", "verified", "failed", "removed")


class CustomDomain(UUIDPk, Timestamps, Base):
    """Platform table (no RLS, no PII; migration 20261003_220_03): a tenant's Career Portal host. One live
    (non-removed) row per host and per tenant. Read through ``platform_service`` with explicit filters."""

    __tablename__ = "custom_domains"
    __table_args__ = (
        CheckConstraint(_in("status", CUSTOM_DOMAIN_STATUSES), name="status"),
        Index("uq_custom_domains_host_live", "host", unique=True, postgresql_where=text("status <> 'removed'")),
        Index("uq_custom_domains_tenant_live", "tenant_id", unique=True, postgresql_where=text("status <> 'removed'")),
    )

    host: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    txt_token: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'pending'"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
