# ruff: file-ignore[line-too-long]
"""Real telephony and speech-to-text (Epic 76, FR-391..FR-395; migrations ``20261010_510_01_telephony`` and
``20261010_510_02_stt_jobs``). Owner: ``app.services.telephony``.

* ``telephony_settings`` -- one row per tenant: calling hours, the daily cost cap, concurrency, the recording notice
  and automatic transcription (audited).
* ``telephony_numbers`` -- the Tenant's registered caller IDs (virtual numbers) per legal entity (audited).
* ``user_call_phones`` -- a recruiter's own phone, sealed with the vault field encryption (only ``last4`` is shown).
* ``voice_calls`` -- one bridged call attempt on a P4 Voice Session (lifecycle audited explicitly).
* ``stt_jobs`` -- transcription jobs for call recordings, P4 uploads and interview captures.

Recording URLs, provider refs and the per-call answer token (stored as a hash) never leave the service layer.
"""

import uuid
from datetime import datetime, time

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

PROVIDER_KEYS = ("exotel", "twilio", "plivo", "fake")
NUMBER_STATUSES = ("active", "inactive")
PHONE_STATUSES = ("pending", "verified", "revoked")
VERIFICATION_METHODS = ("otp_sms", "admin_attested")
CALL_STATUSES = (
    "requested", "refused", "dialing_recruiter", "dialing_candidate", "in_progress", "completed", "no_answer", "busy",
    "failed", "cancelled",
)  # fmt: skip
TERMINAL_CALL_STATUSES = ("refused", "completed", "no_answer", "busy", "failed", "cancelled")
OPEN_CALL_STATUSES = ("requested", "dialing_recruiter", "dialing_candidate", "in_progress")
RECORDING_UNAVAILABLE = ("policy_not_permitted", "consent_missing", "notice_unavailable", "provider_unsupported")
FETCH_STATUSES = ("none", "pending", "stored", "failed", "skipped")
DELETE_STATUSES = ("not_needed", "pending", "deleted", "failed", "unsupported")
STT_SOURCE_KINDS = ("voice_call", "voice_upload", "interview_capture")
STT_STATUSES = ("queued", "running", "completed", "failed", "skipped")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


class TelephonySettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "telephony_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", name="uq_telephony_settings_tenant_id"),
        _fk("updated_by", "users", "fk_telephony_settings_updated_by_users"),
        CheckConstraint("cost_alert_pct BETWEEN 50 AND 95", name="cost_alert_pct"),
        CheckConstraint("max_concurrent_per_user BETWEEN 1 AND 3", name="max_concurrent"),
        CheckConstraint("max_call_minutes BETWEEN 5 AND 60", name="max_call_minutes"),
        CheckConstraint("daily_cost_cap_minor >= 0", name="daily_cost_cap"),
        CheckConstraint("call_window_end > call_window_start", name="call_window"),
    )

    call_window_start: Mapped[time] = mapped_column(Time, nullable=False, server_default=text("'09:00'"))
    call_window_end: Mapped[time] = mapped_column(Time, nullable=False, server_default=text("'21:00'"))
    daily_cost_cap_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("200000"))
    cost_alert_pct: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("80"))
    max_concurrent_per_user: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    max_call_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("30"))
    recording_notice_text: Mapped[str] = mapped_column(
        String(300),
        nullable=False,
        server_default=text("'This call is recorded for recruitment screening by {tenant}.'"),
    )
    stt_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    stt_language_hints: Mapped[list[str]] = mapped_column(
        ARRAY(String(10)), nullable=False, server_default=text("'{en-IN,hi-IN}'::varchar(10)[]")
    )
    auto_transcribe_uploads: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class TelephonyNumber(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "telephony_numbers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk("installation_id", "app_installations", "fk_telephony_numbers_installation_id_inst"),
        _fk("legal_entity_id", "legal_entities", "fk_telephony_numbers_legal_entity_id_le"),
        CheckConstraint(_in("provider_key", PROVIDER_KEYS), name="provider_key"),
        CheckConstraint(_in("status", NUMBER_STATUSES), name="status"),
        CheckConstraint("rate_per_minute_minor >= 0", name="rate"),
        Index(
            "uq_telephony_numbers_active",
            "tenant_id",
            "legal_entity_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            postgresql_nulls_not_distinct=True,
        ),
    )

    installation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    provider_key: Mapped[str] = mapped_column(String(20), nullable=False)
    number_e164: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    regulatory_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    rate_per_minute_minor: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))


class UserCallPhone(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "user_call_phones"
    __audit_exclude__ = ("phone_cipher", "otp_hash", "phone_hash")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk("user_id", "users", "fk_user_call_phones_user_id_users", "CASCADE"),
        _fk("attested_by", "users", "fk_user_call_phones_attested_by_users"),
        CheckConstraint(_in("status", PHONE_STATUSES), name="status"),
        CheckConstraint(
            f"verification_method IS NULL OR {_in('verification_method', VERIFICATION_METHODS)}", name="method"
        ),
        Index(
            "uq_user_call_phones_live",
            "tenant_id",
            "user_id",
            unique=True,
            postgresql_where=text("status <> 'revoked'"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    phone_cipher: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    phone_last4: Mapped[str] = mapped_column(CHAR(4), nullable=False)
    phone_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'pending'"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    attested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    otp_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    otp_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))


class VoiceCall(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "voice_calls"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id", name="uq_voice_calls_human_id"),
        _fk("voice_session_id", "voice_sessions", "fk_voice_calls_voice_session_id_vs"),
        _fk("placed_by", "users", "fk_voice_calls_placed_by_users"),
        _fk("number_id", "telephony_numbers", "fk_voice_calls_number_id_numbers"),
        CheckConstraint(_in("status", CALL_STATUSES), name="status"),
        CheckConstraint(
            f"recording_unavailable_reason IS NULL OR {_in('recording_unavailable_reason', RECORDING_UNAVAILABLE)}",
            name="recording_reason",
        ),
        CheckConstraint(_in("recording_fetch_status", FETCH_STATUSES), name="fetch_status"),
        CheckConstraint(_in("provider_delete_status", DELETE_STATUSES), name="delete_status"),
        Index("ix_voice_calls_placed_by_status", "tenant_id", "placed_by", "status"),
        Index("ix_voice_calls_voice_session_id", "tenant_id", "voice_session_id"),
        Index("ix_voice_calls_candidate_id", "candidate_id"),
        Index("ix_voice_calls_token_hash", "callback_token_hash"),
        Index(
            "ix_voice_calls_provider_delete",
            "provider_delete_status",
            postgresql_where=text("provider_delete_status IN ('pending', 'failed')"),
        ),
        Index(
            "uq_voice_calls_external_ref",
            "tenant_id",
            "provider_key",
            "external_ref",
            unique=True,
            postgresql_where=text("external_ref IS NOT NULL"),
        ),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    voice_session_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    placed_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    config_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    number_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    provider_key: Mapped[str] = mapped_column(String(20), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'requested'"))
    refusal_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    recording_requested: Mapped[bool] = mapped_column(Boolean, nullable=False)
    recording_unavailable_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    dual_channel: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    billable_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_per_minute_minor: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    reserved_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    cost_estimate_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    provider_price_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    price_currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    provider_recording_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    recording_fetch_status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'none'"))
    provider_delete_status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'not_needed'"))
    provider_delete_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    provider_delete_first_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    callback_token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SttJob(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "stt_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "source_kind", "source_id", "file_id", name="uq_stt_jobs_source"),
        CheckConstraint(_in("source_kind", STT_SOURCE_KINDS), name="source_kind"),
        CheckConstraint(_in("status", STT_STATUSES), name="status"),
        Index("ix_stt_jobs_status_created", "status", "created_at"),
        Index("ix_stt_jobs_candidate_id", "candidate_id"),
    )

    source_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    voice_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    interview_capture_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    provider_key: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'queued'"))
    language_hints: Mapped[list[str]] = mapped_column(
        ARRAY(String(10)), nullable=False, server_default=text("'{}'::varchar(10)[]")
    )
    channels: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    audio_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    billable_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_estimate_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    transcript_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "CALL_STATUSES",
    "OPEN_CALL_STATUSES",
    "PROVIDER_KEYS",
    "TERMINAL_CALL_STATUSES",
    "SttJob",
    "TelephonyNumber",
    "TelephonySettings",
    "UserCallPhone",
    "VoiceCall",
]
