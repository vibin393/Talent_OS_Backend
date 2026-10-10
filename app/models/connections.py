"""Calendar / mailbox OAuth connections (Epic 7, story 7.4, FR-53). Tenant-owned with forced RLS.

Tokens live in the Secrets Vault (owner ``calendar_connection`` = the connection id, names ``access_token`` /
``refresh_token``; Epic 66: 500_05 moved them, 500_06 dropped the columns) and are never returned, logged or
audited. Migration 20261001_72.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

CONNECTION_PROVIDERS = ("google", "microsoft", "fake")
CONNECTION_STATUSES = ("active", "needs_reconnect", "revoked")


class UserConnection(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "user_connections"
    __audit_exclude__ = ("last_error", "account_email")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_user_connections_user_id_users",
            ondelete="CASCADE",
        ),
        CheckConstraint(f"provider IN ({', '.join(repr(p) for p in CONNECTION_PROVIDERS)})", name="provider"),
        CheckConstraint(f"status IN ({', '.join(repr(s) for s in CONNECTION_STATUSES)})", name="status"),
        Index(
            "uq_user_connections_user_provider_live",
            "user_id",
            "provider",
            unique=True,
            postgresql_where=text("status <> 'revoked'"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    account_email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(300)), nullable=False, server_default=text("'{}'"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'active'"))
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
