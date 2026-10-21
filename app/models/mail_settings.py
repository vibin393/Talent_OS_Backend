"""Per-tenant outbound email (SMTP) configuration — one row per tenant (singleton).

Lets each org send from its own mailbox / domain instead of the single shared platform mailer. The SMTP
password is stored Fernet-encrypted at rest (``app.core.security`` ``connector`` purpose, the same mechanism
provider-connector credentials use) in ``smtp_password_encrypted`` and is never returned in plaintext.
Resolved at send time by ``app.core.mailer.get_mailer_for_tenant`` / ``resolve_mailer``.

Tenant-owned (RLS + internal-only org RLS); migration ``20261016_515_01_tenant_mail_settings``.
"""

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    Uuid,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps

MAIL_BACKENDS = ("platform", "smtp")
DEFAULT_SMTP_PORT = 587


class TenantMailSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant (created lazily when first saved). Audited; ``smtp_password_encrypted`` is Fernet
    ciphertext and is auto-excluded from the audit log by the ``password`` name rule (``core.audit``)."""

    __tablename__ = "tenant_mail_settings"
    __audit_entity__ = "tenant_mail_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint("backend IN ('platform', 'smtp')", name="backend"),
        CheckConstraint("smtp_port BETWEEN 1 AND 65535", name="smtp_port"),
        ForeignKeyConstraint(
            ["tenant_id", "updated_by"],
            ["users.tenant_id", "users.id"],
            name="fk_tenant_mail_settings_updated_by_users",
            ondelete="RESTRICT",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    backend: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'platform'"))
    smtp_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_port: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(DEFAULT_SMTP_PORT))
    )
    smtp_username: Mapped[str | None] = mapped_column(String(320), nullable=True)
    smtp_password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    smtp_use_ssl: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    sender_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


__all__ = ["DEFAULT_SMTP_PORT", "MAIL_BACKENDS", "TenantMailSettings"]
