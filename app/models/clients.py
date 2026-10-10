"""Client and contact tables (Epic 2, stories 2.1 / 2.2). Tenant-owned with forced RLS; references use
composite foreign keys ``(tenant_id, x_id) -> x(tenant_id, id)`` (FK checks bypass RLS).

Migration: 20260930_07 (clients, contacts, activities).
"""

import uuid
from datetime import datetime
from typing import Any

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
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

CLIENT_STATUSES = ("prospect", "active", "inactive")
CONTACT_TYPES = ("hiring_manager", "hr", "procurement", "finance", "other")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Client(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """A client account. ``name`` is unique per tenant, case-insensitively (citext)."""

    __tablename__ = "clients"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", CLIENT_STATUSES), name="status"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_id"],
            ["users.tenant_id", "users.id"],
            name="fk_clients_owner_id_users",
            ondelete="RESTRICT",
        ),
        Index("ix_clients_owner_id", "owner_id"),
        # Near-duplicate name warnings (pg_trgm similarity).
        Index("ix_clients_name_trgm", text("(name::text) gin_trgm_ops"), postgresql_using="gin"),
    )

    name: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    industry: Mapped[str | None] = mapped_column(String(200), nullable=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # [{city, state, country}]
    locations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'prospect'"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Contact(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """A person at a client. ``email`` (lower-cased citext) is unique per client."""

    __tablename__ = "contacts"
    __pii_fields__ = ("name", "email", "phone")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("type", CONTACT_TYPES), name="type"),
        ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_contacts_client_id_clients",
            ondelete="RESTRICT",
        ),
        Index("ix_contacts_client_id", "client_id"),
        Index(
            "uq_contacts_client_id_email",
            "client_id",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)  # E.164
    type: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'other'"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
