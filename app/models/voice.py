# ruff: noqa: E501
"""Voice session hub table (Epic 53 contract layer, AD-65; migration ``20261008_400_03_p4_hubs``). Owner: Epic 58, which
ALTERs it in its own branch (``20261020_405_01_voice_sessions``): human id, requester / owner, the consent link, the
confirmed location, call times, the questionnaire, transcript source, recording deletion / withdrawal, the AI summary
and gaps, review and failure columns. Epic 58 adds ``voice_upload_attestations`` and ``voice_settings``.

No recording is stored unless the Recording Policy gate allowed it (CHECK ``recording_file_id IS NULL OR
recording_allowed``). Free text (summary, gaps) stays out of the audit log."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
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
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

SESSION_MODES = ("call", "upload")
SESSION_STATUSES = (
    "planned", "consent_pending", "scheduled", "in_progress", "uploaded", "processing", "ready_for_review", "reviewed",
    "failed", "cancelled",
)  # fmt: skip
OPEN_STATUSES = ("planned", "consent_pending", "scheduled", "in_progress", "uploaded", "processing")
TRANSCRIPT_SOURCES = ("provider", "upload")
ATTESTATION_KINDS = ("recording", "transcript")
PROVIDER_KEYS = ("manual", "fake", "exotel", "twilio", "plivo")  # Epic 76 (510_03): real telephony providers
CHANNELS = ("email", "whatsapp", "sms")
DEFAULT_CHANNEL_ORDER = '\'["email", "whatsapp", "sms"]\'::jsonb'


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class VoiceSession(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "voice_sessions"
    __audit_exclude__ = ("summary", "gaps")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id", name="uq_voice_sessions_human_id"),
        _tenant_fk("candidate_id", "candidates", "fk_voice_sessions_candidate_id_candidates"),
        _tenant_fk("application_id", "applications", "fk_voice_sessions_application_id_applications"),
        _tenant_fk(
            "connector_config_id", "provider_connector_configs", "fk_voice_sessions_connector_config_id_pcc"
        ),
        _tenant_fk("recording_file_id", "stored_files", "fk_voice_sessions_recording_file_id_stored_files"),
        _tenant_fk("transcript_id", "transcripts", "fk_voice_sessions_transcript_id_transcripts"),
        _tenant_fk("agent_run_id", "agent_runs", "fk_voice_sessions_agent_run_id_agent_runs"),
        _tenant_fk("attested_by", "users", "fk_voice_sessions_attested_by_users"),
        # Epic 58 (405_01)
        _tenant_fk("requested_by", "users", "fk_voice_sessions_requested_by_users"),
        _tenant_fk("owner_user_id", "users", "fk_voice_sessions_owner_user_id_users"),
        CheckConstraint(_in("mode", SESSION_MODES), name="mode"),
        CheckConstraint(_in("status", SESSION_STATUSES), name="status"),
        CheckConstraint("recording_file_id IS NULL OR recording_allowed", name="recording_allowed"),
        CheckConstraint(
            f"transcript_source IS NULL OR {_in('transcript_source', TRANSCRIPT_SOURCES)}",
            name="transcript_source",
        ),
        Index("ix_voice_sessions_candidate_id", "candidate_id"),
        Index("ix_voice_sessions_application_id", "application_id"),
        Index(
            "uq_voice_sessions_open",
            "tenant_id",
            "application_id",
            unique=True,
            postgresql_where=text(_in("status", OPEN_STATUSES)),
        ),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(40), nullable=False)
    connector_config_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'planned'"))
    voice_consent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    recording_consent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    jurisdiction: Mapped[str | None] = mapped_column(String(20), nullable=True)
    recording_rule: Mapped[str | None] = mapped_column(String(20), nullable=True)
    recording_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    recording_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    transcript_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    attested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    attested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Epic 58 (405_01)
    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    consent_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    confirmed_country: Mapped[str | None] = mapped_column(CHAR(2), nullable=True)
    confirmed_region: Mapped[str | None] = mapped_column(String(10), nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    script_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    questionnaire_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    questionnaire_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcript_source: Mapped[str | None] = mapped_column(String(10), nullable=True)
    recording_media_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    recording_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    gaps: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    screening_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class VoiceUploadAttestation(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """The uploader's statement for a Manual upload (who, when, statement version, consent obtained, policy followed)
    with the Recording Policy gate decision taken at upload time."""

    __tablename__ = "voice_upload_attestations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk(
            "voice_session_id", "voice_sessions", "fk_voice_upload_attestations_session_id", "CASCADE"
        ),
        _tenant_fk("file_id", "stored_files", "fk_voice_upload_attestations_file_id_stored_files"),
        _tenant_fk("attested_by", "users", "fk_voice_upload_attestations_attested_by_users"),
        CheckConstraint(_in("kind", ATTESTATION_KINDS), name="kind"),
        CheckConstraint("consent_obtained AND policy_followed", name="attested"),
        Index("ix_voice_upload_attestations_session_id", "voice_session_id"),
    )

    voice_session_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    statement_version: Mapped[str] = mapped_column(String(20), nullable=False)
    consent_obtained: Mapped[bool] = mapped_column(Boolean, nullable=False)
    policy_followed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    attested_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    gate_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class VoiceSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant (created lazily with the defaults)."""

    __tablename__ = "voice_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint(_in("default_provider", PROVIDER_KEYS), name="default_provider"),
        CheckConstraint("consent_link_days BETWEEN 1 AND 14", name="consent_link_days"),
        CheckConstraint("max_upload_mb BETWEEN 1 AND 200", name="max_upload_mb"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    default_provider: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'manual'"))
    consent_link_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("7"))
    consent_channel_order: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text(DEFAULT_CHANNEL_ORDER)
    )
    max_upload_mb: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("200"))


__all__ = [
    "ATTESTATION_KINDS",
    "CHANNELS",
    "OPEN_STATUSES",
    "PROVIDER_KEYS",
    "SESSION_MODES",
    "SESSION_STATUSES",
    "TRANSCRIPT_SOURCES",
    "VoiceSession",
    "VoiceSettings",
    "VoiceUploadAttestation",
]
