"""Tenant API keys (Epic 12, FR-75; migration 20261001_124).

* ``api_keys`` -- tenant-owned (RLS), audited. Only the SHA-256 of the key is stored (``key_hash``).
* ``api_key_lookups`` -- platform table (no PII): ``key_hash -> (tenant_id, key_id)`` resolved before the
  tenant session is opened.
* ``api_key_request_log`` -- tenant-owned (RLS), one row per request made with a key, kept 90 days.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk


class ApiKey(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("key_hash"),
        CheckConstraint("rate_limit_per_min BETWEEN 60 AND 6000", name="rate_limit"),
    )
    __audit_exclude__ = ("last_used_at", "last_used_ip")

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    key_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("600"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    rotated_from_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class ApiKeyLookup(Base):
    __tablename__ = "api_key_lookups"

    key_hash: Mapped[bytes] = mapped_column(LargeBinary(32), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    key_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class ApiKeyRequestLog(UUIDPk, TenantMixin, Base):
    __tablename__ = "api_key_request_log"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "key_id"],
            ["api_keys.tenant_id", "api_keys.id"],
            name="fk_api_key_request_log_key_id_api_keys",
            ondelete="CASCADE",
        ),
        Index("ix_api_key_request_log_key_at", "key_id", "at"),
        Index("ix_api_key_request_log_at", "at"),
    )

    key_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    route_template: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
