# ruff: noqa: E501
"""Recruitment marketing (Epic 39, FR-196..FR-201; branch ``p3_campaigns``, band 303).

* ``marketing_settings``: one row per tenant (lazy): frequency cap (3 / 7 days, lower only), send windows (narrow
  only), approval threshold, throttle rates / daily caps, tenant-level auto-pause.
* ``audiences`` / ``audience_sources`` / ``audience_list_entries``: include / exclude Pools, Segments and explicit
  lists; ``audience_snapshots`` / ``audience_members`` freeze the resolved ids at send / enrolment (AD-43).
* ``campaigns`` (one-off) and ``campaign_messages`` (one row per recipient and campaign, or per enrolment step --
  the unique keys make a retry never double-send), ``engagement_events`` and ``campaign_attributions``.
* ``sequences`` / ``sequence_steps`` (versioned) / ``enrollments`` and ``marketing_trigger_caps`` (workflow
  "Enrol in Sequence" daily caps).

Internal only (org RLS with no columns). High-volume tables (messages, events, members) are not audited; bodies and
tokens are never stored on them.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
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

CHANNELS = ("email", "sms", "whatsapp")
AUDIENCE_STATUSES = ("draft", "active", "archived")
SOURCE_MODES = ("include", "exclude")
SOURCE_KINDS = ("pool", "segment")
SNAPSHOT_PURPOSES = ("campaign", "enrollment")
CAMPAIGN_STATUSES = ("draft", "pending_approval", "scheduled", "sending", "paused", "completed", "cancelled")
MESSAGE_STATUSES = (
    "pending", "deferred", "queued", "sent", "delivered", "bounced", "failed", "suppressed", "cancelled",
)  # fmt: skip
ENGAGEMENT_KINDS = (
    "delivered", "bounced", "complained", "opened", "clicked", "replied", "unsubscribed", "applied", "hired",
)  # fmt: skip
PAUSE_REASONS = ("complaint_rate", "bounce_rate", "manual")
SEQUENCE_STATUSES = ("draft", "active", "paused", "archived")
SEQUENCE_BASES = ("marketing", "own_application")
STEP_KINDS = ("send", "wait", "branch", "task", "add_to_pool")
ENROLLMENT_STATUSES = ("active", "paused", "completed", "exited", "suppressed")
EXIT_REASONS = ("replied", "applied", "hired", "unsubscribed", "manual")
ENROLLMENT_ORIGINS = ("user", "workflow")

DEFAULT_SEND_WINDOWS: dict[str, dict[str, str]] = {
    "email": {"start": "08:00", "end": "21:00"},
    "sms": {"start": "09:00", "end": "21:00"},
    "whatsapp": {"start": "09:00", "end": "21:00"},
}


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


# ================================================================ settings (303_01)
class MarketingSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "marketing_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint("frequency_cap_count BETWEEN 1 AND 3", name="cap_count"),
        CheckConstraint("frequency_cap_days = 7", name="cap_days"),
        CheckConstraint("approval_threshold >= 1", name="threshold"),
        CheckConstraint("ab_window_hours BETWEEN 1 AND 72", name="ab_window"),
        CheckConstraint("attribution_window_days = 30", name="attribution"),
        CheckConstraint(
            f"paused_reason IS NULL OR {_in('paused_reason', PAUSE_REASONS)}", name="paused_reason"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    frequency_cap_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("3"))
    frequency_cap_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("7"))
    send_windows: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=lambda: dict(DEFAULT_SEND_WINDOWS)
    )
    approval_threshold: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1000"))
    ab_window_hours: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("4"))
    rate_per_minute: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    daily_cap: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    attribution_window_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("30")
    )
    default_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    footer_contact: Mapped[str | None] = mapped_column(String(500), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    paused_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    last_dormant_scan_on: Mapped[date | None] = mapped_column(Date, nullable=True)


# ================================================================ audiences (303_01)
class Audience(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "audiences"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("owner_id", "users", "fk_audiences_owner_id_users"),
        CheckConstraint(_in("status", AUDIENCE_STATUSES), name="status"),
        Index("ix_audiences_owner_id", "owner_id"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'draft'"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class AudienceSource(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "audience_sources"
    __table_args__ = (
        _tenant_fk("audience_id", "audiences", "fk_audience_sources_audience_id_audiences", "CASCADE"),
        _tenant_fk("talent_pool_id", "talent_pools", "fk_audience_sources_pool_id_talent_pools"),
        _tenant_fk("segment_id", "segments", "fk_audience_sources_segment_id_segments"),
        CheckConstraint(_in("mode", SOURCE_MODES), name="mode"),
        CheckConstraint(_in("kind", SOURCE_KINDS), name="kind"),
        CheckConstraint(
            "(kind = 'pool' AND talent_pool_id IS NOT NULL AND segment_id IS NULL)"
            " OR (kind = 'segment' AND segment_id IS NOT NULL AND talent_pool_id IS NULL)",
            name="one_ref",
        ),
        Index("ix_audience_sources_audience_id", "audience_id"),
    )

    audience_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    talent_pool_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    segment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class AudienceListEntry(TenantMixin, Base):
    __tablename__ = "audience_list_entries"
    __table_args__ = (
        PrimaryKeyConstraint("audience_id", "mode", "candidate_id", name="pk_audience_list_entries"),
        _tenant_fk("audience_id", "audiences", "fk_audience_list_entries_audience", "CASCADE"),
        _tenant_fk("candidate_id", "candidates", "fk_audience_list_entries_candidate", "CASCADE"),
        CheckConstraint(_in("mode", SOURCE_MODES), name="mode"),
        Index("ix_audience_list_entries_candidate_id", "candidate_id"),
    )

    audience_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AudienceSnapshot(UUIDPk, TenantMixin, Base):
    __tablename__ = "audience_snapshots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("audience_id", "audiences", "fk_audience_snapshots_audience_id", "CASCADE"),
        CheckConstraint(_in("purpose", SNAPSHOT_PURPOSES), name="purpose"),
        Index("ix_audience_snapshots_audience_id", "audience_id"),
    )

    audience_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    purpose: Mapped[str] = mapped_column(String(12), nullable=False)
    taken_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class AudienceMember(TenantMixin, Base):
    __tablename__ = "audience_members"
    __table_args__ = (
        PrimaryKeyConstraint("snapshot_id", "candidate_id", name="pk_audience_members"),
        _tenant_fk("snapshot_id", "audience_snapshots", "fk_audience_members_snapshot", "CASCADE"),
        _tenant_fk("candidate_id", "candidates", "fk_audience_members_candidate", "CASCADE"),
        Index("ix_audience_members_candidate_id", "candidate_id"),
    )

    snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


# ================================================================ campaigns (303_02)
class Campaign(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "campaigns"
    __audit_exclude__ = ("email_body_html", "counts")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("audience_id", "audiences", "fk_campaigns_audience_id_audiences"),
        _tenant_fk("snapshot_id", "audience_snapshots", "fk_campaigns_snapshot_id"),
        _tenant_fk("owner_id", "users", "fk_campaigns_owner_id_users"),
        CheckConstraint(_in("channel", CHANNELS), name="channel"),
        CheckConstraint(_in("status", CAMPAIGN_STATUSES), name="status"),
        Index("ix_campaigns_status", "tenant_id", "status"),
        Index("ix_campaigns_owner_id", "owner_id"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    audience_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    email_subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    email_body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("email_templates.id", ondelete="SET NULL"), nullable=True
    )
    channel_template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_templates.id", ondelete="SET NULL"), nullable=True
    )
    job_links: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    ab_test: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class CampaignMessage(UUIDPk, TenantMixin, Timestamps, Base):
    """One marketing delivery. Never holds a body, address or token (only the nonce the token is signed over)."""

    __tablename__ = "campaign_messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("campaign_id", "campaigns", "fk_campaign_messages_campaign_id", "CASCADE"),
        _tenant_fk("candidate_id", "candidates", "fk_campaign_messages_candidate_id", "CASCADE"),
        _tenant_fk("enrollment_id", "enrollments", "fk_campaign_messages_enrollment_id", "CASCADE"),
        _tenant_fk("step_id", "sequence_steps", "fk_campaign_messages_step_id", "CASCADE"),
        CheckConstraint(_in("channel", CHANNELS), name="channel"),
        CheckConstraint(_in("status", MESSAGE_STATUSES), name="status"),
        CheckConstraint(
            "(campaign_id IS NOT NULL AND enrollment_id IS NULL AND step_id IS NULL)"
            " OR (campaign_id IS NULL AND enrollment_id IS NOT NULL AND step_id IS NOT NULL)",
            name="one_owner",
        ),
        Index(
            "uq_campaign_messages_campaign_candidate",
            "campaign_id",
            "candidate_id",
            unique=True,
            postgresql_where=text("campaign_id IS NOT NULL"),
        ),
        Index(
            "uq_campaign_messages_enrollment_step",
            "enrollment_id",
            "step_id",
            unique=True,
            postgresql_where=text("enrollment_id IS NOT NULL"),
        ),
        Index("ix_campaign_messages_candidate_sent", "tenant_id", "candidate_id", "sent_at"),
        Index("ix_campaign_messages_status_defer", "status", "defer_until"),
        Index("ix_campaign_messages_email_message_id", "email_message_id"),
        Index("ix_campaign_messages_channel_message_id", "channel_message_id"),
    )

    campaign_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    enrollment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    variant_key: Mapped[str | None] = mapped_column(String(4), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'pending'"))
    suppression_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    defer_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    channel_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_nonce: Mapped[str] = mapped_column(String(32), nullable=False)
    pixel: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))


class EngagementEvent(UUIDPk, TenantMixin, Base):
    __tablename__ = "engagement_events"
    __table_args__ = (
        _tenant_fk("campaign_message_id", "campaign_messages", "fk_engagement_events_message", "CASCADE"),
        _tenant_fk("candidate_id", "candidates", "fk_engagement_events_candidate_id", "CASCADE"),
        CheckConstraint(_in("kind", ENGAGEMENT_KINDS), name="kind"),
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_engagement_events_dedupe_key"),
        Index("ix_engagement_events_message_kind", "campaign_message_id", "kind"),
        Index("ix_engagement_events_candidate", "tenant_id", "candidate_id", "kind", "occurred_at"),
    )

    campaign_message_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(14), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)


class CampaignAttribution(TenantMixin, Base):
    __tablename__ = "campaign_attributions"
    __table_args__ = (
        PrimaryKeyConstraint("application_id", name="pk_campaign_attributions"),
        _tenant_fk("application_id", "applications", "fk_campaign_attributions_application", "CASCADE"),
        _tenant_fk("campaign_message_id", "campaign_messages", "fk_campaign_attributions_message", "CASCADE"),
        _tenant_fk("candidate_id", "candidates", "fk_campaign_attributions_candidate", "CASCADE"),
        Index("ix_campaign_attributions_campaign_id", "campaign_id"),
        Index("ix_campaign_attributions_sequence_id", "sequence_id"),
    )

    application_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    campaign_message_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sequence_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    touched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attributed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    hired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ================================================================ sequences (303_03)
class Sequence(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "sequences"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("owner_id", "users", "fk_sequences_owner_id_users"),
        CheckConstraint(_in("status", SEQUENCE_STATUSES), name="status"),
        CheckConstraint(_in("basis", SEQUENCE_BASES), name="basis"),
        CheckConstraint("re_enrol_gap_days BETWEEN 0 AND 365", name="gap"),
        CheckConstraint("current_version >= 0", name="version"),
        Index("ix_sequences_owner_id", "owner_id"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'draft'"))
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    basis: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'marketing'"))
    exit_on: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text(
            """'{"replied": true, "applied": true, "hired": true, "unsubscribed": true}'::jsonb"""
        ),
    )
    re_enrol_gap_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("30"))


class SequenceStep(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "sequence_steps"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("sequence_id", "sequences", "fk_sequence_steps_sequence_id", "CASCADE"),
        UniqueConstraint("sequence_id", "version", "position", name="uq_sequence_steps_version_position"),
        CheckConstraint(_in("kind", STEP_KINDS), name="kind"),
        CheckConstraint("position >= 1 AND position <= 20", name="position"),
    )

    sequence_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class Enrollment(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "enrollments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("sequence_id", "sequences", "fk_enrollments_sequence_id", "CASCADE"),
        _tenant_fk("candidate_id", "candidates", "fk_enrollments_candidate_id", "CASCADE"),
        _tenant_fk("application_id", "applications", "fk_enrollments_application_id", "SET NULL"),
        _tenant_fk("snapshot_id", "audience_snapshots", "fk_enrollments_snapshot_id", "SET NULL"),
        CheckConstraint(_in("status", ENROLLMENT_STATUSES), name="status"),
        CheckConstraint(f"exit_reason IS NULL OR {_in('exit_reason', EXIT_REASONS)}", name="exit_reason"),
        CheckConstraint(_in("origin", ENROLLMENT_ORIGINS), name="origin"),
        Index(
            "uq_enrollments_active",
            "sequence_id",
            "candidate_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'paused')"),
        ),
        Index("ix_enrollments_due", "status", "next_run_at"),
        Index("ix_enrollments_candidate_id", "candidate_id"),
    )

    sequence_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'active'"))
    exit_reason: Mapped[str | None] = mapped_column(String(14), nullable=True)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    origin: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'user'"))
    workflow_rule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    enrolled_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MarketingTriggerCap(TenantMixin, Base):
    __tablename__ = "marketing_trigger_caps"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "rule_id", "day", name="pk_marketing_trigger_caps"),
        CheckConstraint("count >= 0", name="count"),
    )

    rule_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
