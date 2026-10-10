# ruff: noqa: E501
"""Secrets Vault (Epic 66, AD-82, FR-336; migration ``20261009_500_03_p5_platform``). Owner: ``app.services.vault``.

* ``vault_data_keys`` (tenant): the tenant data key (DEK) wrapped by a KEK from the KMS port (``kek_id`` ``local:v1``).
* ``vault_secrets`` (tenant, not audited): AES-256-GCM ciphertext under the DEK; AAD binds tenant, owner and name.
  Non-secret metadata only in ``meta``; ``vault.*`` audit entries carry names only.
* ``vault_access_log`` (tenant, not audited, retention 365 days): one row per secret read (caller module + purpose).
* ``platform_secrets`` (platform): platform-wide connector credentials (operator-only, never tenant-visible).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TenantMixin, Timestamps, UUIDPk

OWNER_TYPES = (
    "installation",
    "provider_config",
    "webhook_subscription",
    "calendar_connection",
    "oauth_app_tenant",
)
DEK_STATUSES = ("active", "retired")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class PlatformSecret(UUIDPk, Timestamps, Base):
    __tablename__ = "platform_secrets"
    __table_args__ = (UniqueConstraint("name"),)

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kek_id: Mapped[str] = mapped_column(String(60), nullable=False)
    fingerprint: Mapped[str | None] = mapped_column(String(4), nullable=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class VaultDataKey(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "vault_data_keys"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "version"),
        CheckConstraint(_in("status", DEK_STATUSES), name="status"),
        Index(
            "uq_vault_data_keys_active", "tenant_id", unique=True, postgresql_where=text("status = 'active'")
        ),
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False)
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kek_id: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))
    rewrapped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VaultSecret(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "vault_secrets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "owner_type", "owner_id", "name", name="uq_vault_secrets_owner_name"),
        CheckConstraint(_in("owner_type", OWNER_TYPES), name="owner_type"),
        Index("ix_vault_secrets_owner", "owner_type", "owner_id"),
        Index("ix_vault_secrets_expires_at", "expires_at"),
    )

    owner_type: Mapped[str] = mapped_column(String(30), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    dek_version: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(4), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class VaultAccessLog(UUIDPk, TenantMixin, Base):
    __tablename__ = "vault_access_log"
    __table_args__ = (
        Index("ix_vault_access_log_secret_id_at", "secret_id", "at"),
        Index("ix_vault_access_log_at", "at"),
    )

    secret_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(30), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    purpose: Mapped[str] = mapped_column(String(60), nullable=False)
    caller: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


__all__ = ["DEK_STATUSES", "OWNER_TYPES", "PlatformSecret", "VaultAccessLog", "VaultDataKey", "VaultSecret"]
