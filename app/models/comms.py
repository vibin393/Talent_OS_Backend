"""Communication tables (Epic 10, story 10.1): email templates (+ language variants), sent-email log with
recipients, and per-address delivery status (bounces, marketing opt-out). Tenant-owned with forced RLS.

Migration: 20261001_101 (comms templates and emails).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

TEMPLATE_KEYS = (
    "submission",
    "interview_invite",
    "interview_reminder",
    "rejection",
    "offer",
    "application_confirmation",
    "ai_screening_invite",
    "client_feedback_request",
    "custom",
    "voice_consent_request",  # Epic 58 (405_01, coordinator C-11)
)
TRANSPORTS = ("mailbox_google", "mailbox_microsoft", "platform")
MESSAGE_STATUSES = ("queued", "sent", "failed")
CATEGORIES = ("process", "marketing")
ORIGINS = ("user", "workflow", "system", "campaign", "agent")  # Epic 36 (300_01): +campaign; Epic 53 (400_01): +agent
RECIPIENT_KINDS = ("to", "cc")
RECIPIENT_STATUSES = ("pending", "sent", "bounced", "suppressed")
BOUNCE_TYPES = ("hard", "soft", "complaint")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _user_fk(column: str, table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column],
        ["users.tenant_id", "users.id"],
        name=f"fk_{table}_{column}_users",
        ondelete="RESTRICT",
    )


class EmailTemplate(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "email_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", "name"),
        CheckConstraint(_in("key", TEMPLATE_KEYS), name="key"),
    )

    key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmailTemplateVariant(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    """One language version of a template (launch: ``en`` only). Every edit bumps ``version``."""

    __tablename__ = "email_template_variants"
    __table_args__ = (UniqueConstraint("template_id", "language"),)

    template_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("email_templates.id", ondelete="CASCADE"), nullable=False
    )
    language: Mapped[str] = mapped_column(String(5), nullable=False, server_default=text("'en'"))
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class Email(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    """One outgoing message (``email_messages``). Bodies are never audited or logged."""

    __tablename__ = "email_messages"
    __pii_fields__ = ("subject", "from_address", "reply_to")
    __audit_exclude__ = ("body_html", "body_text", "related")
    __audit_entity__ = "email_message"
    __table_args__ = (
        _user_fk("sender_user_id", "email_messages"),
        CheckConstraint(_in("transport", TRANSPORTS), name="transport"),
        CheckConstraint(_in("status", MESSAGE_STATUSES), name="status"),
        CheckConstraint(_in("category", CATEGORIES), name="category"),
        CheckConstraint(_in("origin", ORIGINS), name="origin"),
        Index("ix_email_messages_sender_created", "tenant_id", "sender_user_id", "created_at"),
        Index("ix_email_messages_provider_message_id", "provider_message_id"),
        Index("ix_email_messages_related", "related", postgresql_using="gin"),
    )

    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    transport: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'platform'"))
    fallback_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    from_address: Mapped[str] = mapped_column(String(320), nullable=False)
    reply_to: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    template_variant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'process'"))
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'queued'"))
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # [{entity_type, entity_id}] the message is filed under (activity entries, entity email lists).
    related: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'[]'::jsonb")
    )
    origin: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'user'"))
    workflow_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class EmailRecipient(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "email_recipients"
    __pii_fields__ = ("address", "bounce_reason")
    __table_args__ = (
        CheckConstraint(_in("kind", RECIPIENT_KINDS), name="kind"),
        CheckConstraint(_in("status", RECIPIENT_STATUSES), name="status"),
        CheckConstraint(f"bounce_type IS NULL OR {_in('bounce_type', BOUNCE_TYPES)}", name="bounce_type"),
        Index("ix_email_recipients_message_id", "message_id"),
        Index("ix_email_recipients_address", "tenant_id", "address"),
        Index("ix_email_recipients_entity", "tenant_id", "entity_type", "entity_id"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("email_messages.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(4), nullable=False, server_default=text("'to'"))
    address: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'pending'"))
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bounce_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    bounce_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class EmailAddressStatus(Timestamps, AuditedMixin, Base):
    """Delivery facts about one address in a tenant (PK ``(tenant_id, address)``)."""

    __tablename__ = "email_address_status"
    __pii_fields__ = ("address", "last_bounce_reason")
    __audit_entity_id__ = "id"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    address: Mapped[str] = mapped_column(CITEXT(), primary_key=True)
    # Surrogate id: audit entity id (the address itself is personal data).
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True, default=uuid.uuid4)
    hard_bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_bounce_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    marketing_opt_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ================================================================ Phase 2 Epic 33 (FR-171..FR-173)
# Migrations 20261003_220_01 (inbound email) and 20261003_220_02 (channels). Org RLS: internal-only, except
# ``channel_messages`` / ``channel_opt_outs`` which carry ``candidate_id`` for the candidate portal (worker scope).
THREAD_STATUSES = ("active", "stopped", "error")
CHANNELS = ("sms", "whatsapp")
OPT_OUT_CHANNELS = (*CHANNELS, "voice")  # Epic 76: Do-Not-Call (``channel_opt_outs`` only; 510_03)
CHANNEL_PURPOSES = (
    "timesheet_reminder",
    "onboarding_task",
    "interview_reminder",
    "joining_touchpoint",
    "other_transactional",
    "marketing",  # Epic 36 (300_01): promotional DLT / WhatsApp templates (Epic 39)
)
CHANNEL_TEMPLATE_STATUSES = ("draft", "registered", "archived")
CHANNEL_DIRECTIONS = ("outbound", "inbound")
CHANNEL_MESSAGE_STATUSES = (
    "queued",
    "deferred_quiet_hours",
    "sent",
    "delivered",
    "read",
    "failed",
    "refused",
    "received",
    "manual_pending",
)
CHANNEL_RECIPIENT_KINDS = ("candidate", "contact", "user")
OPT_OUT_SOURCES = ("stop_keyword", "recipient_link", "user", "provider", "candidate_portal")
DEFAULT_QUIET_HOURS = {"start": "21:00", "end": "09:00", "tz": "Asia/Kolkata"}


class CommsChannelSettings(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    """One row per tenant (created lazily): SMS / WhatsApp switches, DLT entity, quiet hours, reply capture."""

    __tablename__ = "comms_channel_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id"),
        CheckConstraint("thread_sync_days BETWEEN 7 AND 90", name="thread_sync_days"),
    )

    sms_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    whatsapp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    default_sms_sender_id: Mapped[str | None] = mapped_column(String(6), nullable=True)
    dlt_entity_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    quiet_hours: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False, default=lambda: dict(DEFAULT_QUIET_HOURS))
    inbound_sync_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    thread_sync_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))


class EmailThread(UUIDPk, Timestamps, TenantMixin, Base):
    """A provider thread started by a mailbox-sent email whose replies are captured (FR-171)."""

    __tablename__ = "email_threads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "thread_ref", "user_id"),
        CheckConstraint(_in("status", THREAD_STATUSES), name="status"),
        Index("ix_email_threads_active", "tenant_id", "status", "sync_until"),
        Index("ix_email_threads_email_message_id", "email_message_id"),
    )

    email_message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("email_messages.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_connections.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    thread_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    rfc_message_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    related: Mapped[list[dict[str, Any]]] = mapped_column(JSONB(), nullable=False, server_default=text("'[]'::jsonb"))
    sync_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InboundEmail(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    """A captured reply (text only, <= 100 KB; attachment names only). Never rendered as HTML."""

    __tablename__ = "inbound_emails"
    __pii_fields__ = ("from_address", "subject")
    __audit_exclude__ = ("body_text", "related", "attachment_names")
    __audit_entity__ = "inbound_email"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider_message_id"),
        Index("ix_inbound_emails_thread_id", "thread_id"),
        Index("ix_inbound_emails_related", "related", postgresql_using="gin"),
    )

    thread_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("email_threads.id", ondelete="CASCADE"), nullable=False
    )
    email_message_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    from_address: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False, server_default=text("''"))
    body_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attachment_names: Mapped[list[str]] = mapped_column(ARRAY(String(255)), nullable=False, server_default=text("'{}'"))
    related: Mapped[list[dict[str, Any]]] = mapped_column(JSONB(), nullable=False, server_default=text("'[]'::jsonb"))


class ChannelTemplate(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    """An SMS (DLT) or WhatsApp (approved) template. Sendable only when ``registered`` with a provider ref."""

    __tablename__ = "channel_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "channel", "key"),
        CheckConstraint(_in("channel", CHANNELS), name="channel"),
        CheckConstraint(_in("purpose", CHANNEL_PURPOSES), name="purpose"),
        CheckConstraint(_in("status", CHANNEL_TEMPLATE_STATUSES), name="status"),
    )

    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[list[str]] = mapped_column(ARRAY(String(80)), nullable=False, server_default=text("'{}'"))
    provider_template_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    language: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'en'"))
    sender_id: Mapped[str | None] = mapped_column(String(6), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'draft'"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class ChannelMessage(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    """One outbound or inbound SMS / WhatsApp message. Phone numbers and bodies are never logged/audited."""

    __tablename__ = "channel_messages"
    __pii_fields__ = ("to_e164",)
    __audit_exclude__ = ("to_e164", "body_rendered", "variables", "related")
    __audit_entity__ = "channel_message"
    __table_args__ = (
        CheckConstraint(_in("channel", CHANNELS), name="channel"),
        CheckConstraint(_in("direction", CHANNEL_DIRECTIONS), name="direction"),
        CheckConstraint(_in("status", CHANNEL_MESSAGE_STATUSES), name="status"),
        CheckConstraint(_in("origin", ORIGINS), name="origin"),
        CheckConstraint(
            f"recipient_kind IS NULL OR {_in('recipient_kind', CHANNEL_RECIPIENT_KINDS)}", name="recipient_kind"
        ),
        Index("ix_channel_messages_recipient", "tenant_id", "recipient_kind", "recipient_id", "created_at"),
        Index("ix_channel_messages_phone", "tenant_id", "channel", "to_e164", "created_at"),
        Index("ix_channel_messages_provider_message_id", "provider_message_id"),
        Index("ix_channel_messages_status", "tenant_id", "status"),
    )

    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'outbound'"))
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_templates.id", ondelete="SET NULL"), nullable=True
    )
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recipient_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    to_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)
    body_rendered: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    variables: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'queued'"))
    refusal_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    connector_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    dispatch_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    related: Mapped[list[dict[str, Any]]] = mapped_column(JSONB(), nullable=False, server_default=text("'[]'::jsonb"))
    origin: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'user'"))
    workflow_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sent_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deferred_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    in_reply_to_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class ChannelOptOut(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    """Suppression of a phone on a channel. ``phone_hash`` (HMAC) survives erasure; ``phone_e164`` does not."""

    __tablename__ = "channel_opt_outs"
    __pii_fields__ = ("phone_e164",)
    __audit_exclude__ = ("phone_e164", "phone_hash")
    __audit_entity__ = "channel_opt_out"
    __table_args__ = (
        CheckConstraint(_in("channel", OPT_OUT_CHANNELS), name="channel"),
        CheckConstraint(_in("source", OPT_OUT_SOURCES), name="source"),
        CheckConstraint(
            f"recipient_kind IS NULL OR {_in('recipient_kind', CHANNEL_RECIPIENT_KINDS)}", name="recipient_kind"
        ),
        Index(
            "uq_channel_opt_outs_active",
            "tenant_id",
            "channel",
            "phone_hash",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_channel_opt_outs_candidate_id", "candidate_id"),
    )

    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    phone_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)
    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
