# ruff: noqa: E501
"""Voice Screening DTOs (Epic 58): sessions, uploads, playback, speaker labels, the Recording Policy admin and the
tenant voice settings. No transcript text and no audio ever appear here (the review reuses Epic 57's answer DTOs)."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ConsentState = Literal["granted", "refused", "pending"]
SessionStatus = Literal[
    "planned", "consent_pending", "scheduled", "in_progress", "uploaded", "processing", "ready_for_review", "reviewed",
    "failed", "cancelled",
]  # fmt: skip
RecordingRule = Literal["not_permitted", "candidate_consent", "all_party_consent"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VoiceSessionRequestIn(_In):
    mode: Literal["call", "upload"] = "call"
    provider_key: Literal["manual", "fake"] | None = None
    script_ref: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_\-]{1,80}$")] | None = None


class VoiceSessionOut(BaseModel):
    id: uuid.UUID
    human_id: str
    application_id: uuid.UUID | None
    candidate_id: uuid.UUID
    mode: str
    provider_key: str
    status: SessionStatus
    external_ref: str | None = None
    voice_consent: ConsentState
    recording_consent: ConsentState
    jurisdiction: str | None
    recording_rule: str | None
    recording_allowed: bool
    recording_block_reason: str | None = None
    has_recording: bool
    recording_media_type: str | None = None
    recording_deleted_at: datetime | None = None
    has_transcript: bool
    transcript_id: uuid.UUID | None
    transcript_source: str | None
    needs_speaker_label: bool = False
    questionnaire_id: uuid.UUID | None
    questionnaire_version: int | None
    summary: str | None = None
    gaps: list[Any] = Field(default_factory=list)
    failure_code: str | None = None
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_s: int | None = None
    requested_by: uuid.UUID | None = None
    owner_user_id: uuid.UUID
    reviewed_by: uuid.UUID | None = None
    reviewed_at: datetime | None = None
    screening_id: uuid.UUID | None = None
    withdrawn_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class SpeakerOut(BaseModel):
    seq: int
    speaker_role: str
    start_ms: int | None
    end_ms: int | None
    text: str


class VoiceTranscriptOut(BaseModel):
    transcript_id: uuid.UUID
    status: str
    segments: list[SpeakerOut]


class SpeakerLabelIn(_In):
    segment_seqs_as_candidate: list[int] = Field(min_length=1, max_length=2000)


class PlaybackOut(BaseModel):
    url: str
    expires_in: int
    media_type: str | None


class RecordingPolicyIn(_In):
    country: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=60)]
    region: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] | None = None
    rule: RecordingRule
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


class RecordingPolicyOut(BaseModel):
    id: uuid.UUID
    country: str
    region: str | None
    rule: RecordingRule
    note: str | None
    updated_by: uuid.UUID | None
    updated_by_name: str | None = None
    updated_at: datetime


class PolicyEvaluationOut(BaseModel):
    country: str | None
    region: str | None
    jurisdiction: str | None
    rule: RecordingRule
    matched: Literal["region", "country", "none"]


class VoiceSettingsIn(_In):
    default_provider: Literal["manual", "fake"] | None = None
    consent_link_days: int | None = Field(default=None, ge=1, le=14)
    consent_channel_order: list[Literal["email", "whatsapp", "sms"]] | None = Field(
        default=None, min_length=1, max_length=3
    )
    max_upload_mb: int | None = Field(default=None, ge=1, le=200)


class VoiceSettingsOut(BaseModel):
    default_provider: Literal["manual", "fake"]
    consent_link_days: int
    consent_channel_order: list[str]
    max_upload_mb: int
    fake_available: bool


class VoiceResult(BaseModel):
    """``emit_result`` of the Voice Screening Agent (ids and counts only)."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal[
        "done",
        "needs_human",
        "nothing_to_do",
        "consent_requested",
        "scheduled",
        "manual_required",
        "ready_for_review",
    ]
    summary: str = Field(default="", max_length=2000)
    answered: int = Field(default=0, ge=0)
    unanswered_required: list[uuid.UUID] = Field(default_factory=list, max_length=100)


__all__ = [
    "ConsentState",
    "PlaybackOut",
    "PolicyEvaluationOut",
    "RecordingPolicyIn",
    "RecordingPolicyOut",
    "SpeakerLabelIn",
    "SpeakerOut",
    "VoiceResult",
    "VoiceSessionOut",
    "VoiceSessionRequestIn",
    "VoiceSettingsIn",
    "VoiceSettingsOut",
    "VoiceTranscriptOut",
]
