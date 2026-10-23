# ruff: file-ignore[line-too-long]
"""Calendar Sync (Epic 72, AD-81, FR-371..FR-374; migration ``20261010_506_01``). Owner: ``app.services.calendar_sync``.

Tenant tables (RLS + internal-only org RLS):

* ``calendar_channels`` (not audited; lifecycle audited explicitly): one push channel per watched connection (Google
  ``events.watch`` channel / Graph subscription), its opaque sync token and the fallback polling schedule. The channel
  secret lives in the vault (owner ``calendar_connection``, name ``channel_token``).
* ``calendar_event_links`` (not audited): Talent OS-created provider events (organiser event + attendee copies matched by
  iCalUID), the last version tag / etag written and the inbound state.
* ``calendar_sync_settings`` (audited; one row per tenant) and ``calendar_free_busy_optouts`` (audited).

Platform table ``calendar_channel_lookups``: channel id -> tenant for the public push endpoint (no PII).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

CHANNEL_PROVIDERS = ("google", "microsoft", "fake")
CHANNEL_STATUSES = ("active", "renewing", "polling", "stopped", "failed")
LIVE_CHANNEL_STATUSES = ("active", "renewing", "polling")
LINK_ROLES = ("organizer", "attendee")
LINK_STATES = ("in_sync", "moved", "deleted", "restoring", "conflict", "detached")
SEEN_STATUSES = ("confirmed", "tentative", "cancelled", "deleted")
MOVE_POLICIES = ("restore", "keep_as_proposal")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


class CalendarChannel(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "calendar_channels"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "channel_id", name="uq_calendar_channels_tenant_id_channel_id"),
        _tenant_fk("connection_id", "user_connections", "fk_calendar_channels_connection_id_user_connections", "CASCADE"),
        _tenant_fk("user_id", "users", "fk_calendar_channels_user_id_users", "CASCADE"),
        CheckConstraint(_in("provider", CHANNEL_PROVIDERS), name="provider"),
        CheckConstraint(_in("status", CHANNEL_STATUSES), name="status"),
        Index(
            "uq_calendar_channels_live", "tenant_id", "connection_id", unique=True,
            postgresql_where=text("status IN ('active','renewing','polling')"),
        ),
        Index("ix_calendar_channels_renew", "status", "renew_after"),
        Index("ix_calendar_channels_poll", "status", "next_poll_at"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(10), nullable=False)
    channel_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider_resource_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_notification_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    renew_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CalendarChannelLookup(Base):
    """Platform table: push channel id -> tenant (no PII)."""

    __tablename__ = "calendar_channel_lookups"

    channel_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class CalendarEventLink(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "calendar_event_links"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "connection_id", "provider_event_id", name="uq_calendar_event_links_event"),
        _tenant_fk("interview_id", "interviews", "fk_calendar_event_links_interview_id_interviews", "CASCADE"),
        _tenant_fk("participant_id", "interview_participants", "fk_calendar_event_links_participant_id_interview_participants", "CASCADE"),
        CheckConstraint(_in("provider", CHANNEL_PROVIDERS), name="provider"),
        CheckConstraint(_in("role", LINK_ROLES), name="role"),
        CheckConstraint(_in("state", LINK_STATES), name="state"),
        CheckConstraint(f"seen_status IS NULL OR {_in('seen_status', SEEN_STATUSES)}", name="seen_status"),
        Index("ix_calendar_event_links_interview_id", "interview_id"),
        Index("ix_calendar_event_links_connection_ical", "connection_id", "ical_uid"),
    )

    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    participant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(10), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(500), nullable=False)
    ical_uid: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    tos_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    tos_tag: Mapped[str] = mapped_column(String(100), nullable=False)
    tos_etag: Mapped[str | None] = mapped_column(String(200), nullable=True)
    seen_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seen_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seen_status: Mapped[str | None] = mapped_column(String(12), nullable=True)
    state: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'in_sync'"))
    restore_count_window: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CalendarSyncSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "calendar_sync_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_calendar_sync_settings_tenant_id"),
        CheckConstraint(_in("move_policy", MOVE_POLICIES), name="move_policy"),
        CheckConstraint("poll_minutes BETWEEN 5 AND 60", name="poll_minutes"),
        CheckConstraint("restore_limit_per_hour BETWEEN 1 AND 10", name="restore_limit_per_hour"),
    )

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    move_policy: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'restore'"))
    poll_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("15"))
    restore_limit_per_hour: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("3"))
    org_source_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class CalendarFreeBusyOptout(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "calendar_free_busy_optouts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_calendar_free_busy_optouts_user"),
        _tenant_fk("user_id", "users", "fk_calendar_free_busy_optouts_user_id_users", "CASCADE"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    opted_out_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
