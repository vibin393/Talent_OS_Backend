# ruff: noqa: E501
"""Telephony API DTOs (Epic 76, spec A8). Responses never carry phone numbers (beyond the recruiter's ``last4`` and
the Tenant's caller ID label), provider refs, recording URLs or answer tokens."""

import uuid
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RefusalCode = Literal[
    "human_only_action",
    "ecosystem_suspended",
    "no_voice_provider",
    "call_phone_not_verified",
    "voice_consent_required",
    "do_not_call",
    "outside_calling_hours",
    "call_in_progress",
    "cost_cap_reached",
    "no_phone",
]
CallStatus = Literal[
    "requested", "refused", "dialing_recruiter", "dialing_candidate", "in_progress", "completed", "no_answer", "busy",
    "failed", "cancelled",
]  # fmt: skip


class TelephonySettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_window_start: time | None = None
    call_window_end: time | None = None
    daily_cost_cap_minor: int | None = Field(default=None, ge=0, le=100_000_000)
    cost_alert_pct: int | None = Field(default=None, ge=50, le=95)
    max_concurrent_per_user: int | None = Field(default=None, ge=1, le=3)
    max_call_minutes: int | None = Field(default=None, ge=5, le=60)
    recording_notice_text: str | None = Field(default=None, min_length=10, max_length=300)
    stt_enabled: bool | None = None
    stt_language_hints: list[Literal["en-IN", "hi-IN"]] | None = Field(default=None, min_length=1)
    auto_transcribe_uploads: bool | None = None

    @model_validator(mode="after")
    def _no_nulls(self) -> "TelephonySettingsIn":
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                msg = f"{name} cannot be empty"
                raise ValueError(msg)
        return self


class TelephonySettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    call_window_start: time
    call_window_end: time
    daily_cost_cap_minor: int
    cost_alert_pct: int
    max_concurrent_per_user: int
    max_call_minutes: int
    recording_notice_text: str
    stt_enabled: bool
    stt_language_hints: list[str]
    auto_transcribe_uploads: bool
    updated_at: datetime | None = None


class TelephonyNumberIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    installation_id: uuid.UUID
    legal_entity_id: uuid.UUID | None = None
    number_e164: str = Field(min_length=8, max_length=16)
    label: str | None = Field(default=None, max_length=80)
    regulatory_ref: str | None = Field(default=None, max_length=120)
    rate_per_minute_minor: int = Field(default=0, ge=0, le=100_000)


class TelephonyNumberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=80)
    regulatory_ref: str | None = Field(default=None, max_length=120)
    rate_per_minute_minor: int | None = Field(default=None, ge=0, le=100_000)
    status: Literal["active", "inactive"] | None = None


class TelephonyNumberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    installation_id: uuid.UUID
    legal_entity_id: uuid.UUID | None
    provider_key: str
    number_e164: str  # the Tenant's caller ID (business data, not personal)
    label: str | None
    regulatory_ref: str | None
    rate_per_minute_minor: int
    status: str
    created_at: datetime


class CallPhoneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone_e164: str = Field(min_length=8, max_length=20)


class CallPhoneVerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class CallPhoneAttestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone_e164: str = Field(min_length=8, max_length=20)
    reason: str = Field(min_length=3, max_length=300)


class CallPhoneOut(BaseModel):
    status: Literal["none", "pending", "verified"]
    last4: str | None = None
    verified_at: datetime | None = None
    verification_method: str | None = None
    otp_expires_at: datetime | None = None


class ConsentFlags(BaseModel):
    voice_screening: bool
    call_recording: bool


class CallOptionsOut(BaseModel):
    can_call: bool
    refusal_code: RefusalCode | None = None
    refusal_message: str | None = None
    next_window_at: datetime | None = None
    provider_key: str | None = None
    caller_id_label: str | None = None
    recording_will_be: Literal["recorded", "not_recorded"] = "not_recorded"
    recording_reason: str | None = None
    consent: ConsentFlags
    phone_verified: bool
    phone_last4: str | None = None
    today_cost_pct: int = 0
    voice_session_id: uuid.UUID | None = None
    manual_available: bool = True


class PlaceCallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice_session_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None
    candidate_id: uuid.UUID | None = None
    phone_choice: Literal["primary", "secondary"] = "primary"

    @model_validator(mode="after")
    def _one_subject(self) -> "PlaceCallIn":
        given = [x for x in (self.voice_session_id, self.application_id, self.candidate_id) if x is not None]
        if len(given) != 1:
            msg = "Give exactly one of voice_session_id, application_id or candidate_id"
            raise ValueError(msg)
        return self


class VoiceCallOut(BaseModel):
    id: uuid.UUID
    human_id: str
    voice_session_id: uuid.UUID
    application_id: uuid.UUID | None
    candidate_id: uuid.UUID | None
    placed_by: uuid.UUID
    provider_key: str
    status: CallStatus
    refusal_code: str | None
    failure_code: str | None
    recorded: bool
    recording_unavailable_reason: str | None
    recording_status: Literal["none", "pending", "stored", "failed", "skipped"]
    transcript_status: Literal["none", "queued", "running", "completed", "failed", "skipped"]
    provider_delete_status: str
    requested_at: datetime
    answered_at: datetime | None
    ended_at: datetime | None
    duration_s: int | None
    billable_minutes: int | None
    cost_estimate_minor: int | None
    manual_instructions: str | None = None


class UsageRow(BaseModel):
    key: str
    label: str | None = None
    calls: int
    minutes: int
    stt_minutes: int
    cost_estimate_minor: int


class UsageOut(BaseModel):
    from_date: date
    to_date: date
    group_by: Literal["user", "legal_entity", "day"]
    rows: list[UsageRow]
    today_cost_minor: int
    daily_cost_cap_minor: int
    today_cost_pct: int


class DoNotCallOut(BaseModel):
    recorded: bool
