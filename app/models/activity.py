"""Activity timeline table (Epic 2, story 2.3): notes, logged calls/meetings, emails and system entries
for any entity (``entity_type`` + ``entity_id``; access is decided by the owning service's registered
checker, see ``app.services.activity_service.register_entity``). Tenant-owned with forced RLS.

Migration: 20260930_07 (clients, contacts, activities).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, UUIDPk

ACTIVITY_KINDS = ("note", "call", "meeting", "email", "system", "email_reply", "sms", "whatsapp")  # Epic 33: +3 kinds
USER_KINDS = ("note", "call", "meeting")  # created / edited by users through the API


class Activity(UUIDPk, TenantMixin, AuditedMixin, Base):
    __tablename__ = "activities"
    __pii_fields__ = ("body", "participants")
    __table_args__ = (
        CheckConstraint(f"kind IN ({', '.join(repr(k) for k in ACTIVITY_KINDS)})", name="kind"),
        ForeignKeyConstraint(
            ["tenant_id", "author_id"],
            ["users.tenant_id", "users.id"],
            name="fk_activities_author_id_users",
            ondelete="RESTRICT",
        ),
        Index("ix_activities_entity", "entity_type", "entity_id", "occurred_at", "id"),
        Index("ix_activities_author_id", "author_id"),
    )

    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str] = mapped_column(Text(), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    author_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)  # NULL = system
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # [{kind: user|contact|other, id?, name?}]
    participants: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
    mentions: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    # Structured metadata of system / email entries (ids, counts; never PII).
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
