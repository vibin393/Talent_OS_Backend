"""Notifications (Epic 10, story 10.3, FR-57): in-app notifications, their email/digest deliveries, per-type
channel preferences and per-user digest settings. Tenant-owned with forced RLS.

``notifications`` and ``notification_deliveries`` are high-volume and deliberately NOT audited.
``notifications.in_app`` is false for rows created only to deliver an email (preference ``email``): they keep
fan-out idempotent (``dedupe_key``) but never show in the bell.

Migration: 20261001_102 (tasks and notifications).

Epic 13 (AD-31, migration 20261002_200_05): notifications address ``recipient_kind`` (user | candidate |
client_user | vendor_user) + ``recipient_id``; ``user_id`` is set for users only. External recipients carry
their organization column (``client_id`` / ``vendor_id`` / ``candidate_id``) for org RLS; deliveries inherit
those columns from their notification (``tos_inherit_org``).
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

CHANNELS = ("in_app", "email", "both", "off")
DELIVERY_CHANNELS = ("email", "digest")
DELIVERY_STATUSES = ("queued", "sent", "skipped", "failed")
RECIPIENT_KINDS = ("user", "candidate", "client_user", "vendor_user")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _recipient_default(context: Any) -> Any:
    """Phase 1 inserts set only ``user_id``: the recipient is that user (Epic 13)."""
    return context.get_current_parameters().get("user_id")


class Notification(UUIDPk, TenantMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_notifications_user_id_users",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id", "recipient_kind", "recipient_id", "dedupe_key", name="uq_notifications_recipient_dedupe"
        ),
        Index(
            "ix_notifications_unread",
            "tenant_id",
            "user_id",
            text("created_at DESC"),
            postgresql_where=text("read_at IS NULL AND in_app"),
        ),
        Index("ix_notifications_user_created", "tenant_id", "user_id", "created_at"),
        Index("ix_notifications_recipient_created", "tenant_id", "recipient_kind", "recipient_id", "created_at"),
        CheckConstraint(_in("recipient_kind", RECIPIENT_KINDS), name="recipient_kind"),
        CheckConstraint("recipient_kind <> 'user' OR user_id = recipient_id", name="user_recipient"),
        CheckConstraint("(recipient_kind = 'user') = (user_id IS NOT NULL)", name="user_id_for_users"),
        CheckConstraint(
            "((client_id IS NOT NULL) = (recipient_kind = 'client_user')) "
            "AND ((vendor_id IS NOT NULL) = (recipient_kind = 'vendor_user')) "
            "AND ((candidate_id IS NOT NULL) = (recipient_kind = 'candidate'))",
            name="org_columns",
        ),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    recipient_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'user'"))
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, default=_recipient_default
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    link_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    in_app: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NotificationDelivery(UUIDPk, TenantMixin, Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint(_in("channel", DELIVERY_CHANNELS), name="channel"),
        CheckConstraint(_in("status", DELIVERY_STATUSES), name="status"),
        Index("ix_notification_deliveries_notification_id", "notification_id"),
    )

    notification_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'queued'"))
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Epic 13: copied from the notification by ``tos_inherit_org`` (org RLS).
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class NotificationPreference(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    """A user's channel for one notification type; a missing row means the registry default."""

    __tablename__ = "notification_preferences"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_notification_preferences_user_id_users",
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "user_id", "type"),
        CheckConstraint(_in("channel", CHANNELS), name="channel"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    channel: Mapped[str] = mapped_column(String(10), nullable=False)


class NotificationSettings(Timestamps, AuditedMixin, Base):
    """Per-user digest settings (PK ``(tenant_id, user_id)``). The digest hour is in the user's local time
    (users carry no timezone yet, so the tenant timezone applies)."""

    __tablename__ = "notification_settings"
    __audit_entity_id__ = "user_id"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_notification_settings_user_id_users",
            ondelete="CASCADE",
        ),
        CheckConstraint("digest_hour_local BETWEEN 0 AND 23", name="digest_hour"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    digest_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    digest_hour_local: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("8"))
    last_digest_date: Mapped[date | None] = mapped_column(Date, nullable=True)
