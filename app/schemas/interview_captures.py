"""Interview Assistant DTOs (Epic 60): capture status and consent, uploads, outputs, drafts and settings."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

CaptureStatus = Literal["planned", "consent_pending", "blocked", "uploaded", "processing", "ready", "erased"]
ConsentState = Literal["agreed", "waiting", "refused"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParticipantConsentOut(BaseModel):
    participant_id: uuid.UUID | None
    kind: Literal["candidate", "internal", "external"]
    name: str
    user_id: uuid.UUID | None = None
    state: ConsentState


class CaptureStatusOut(BaseModel):
    interview_id: uuid.UUID
    capture_id: uuid.UUID | None
    status: CaptureStatus | Literal["not_requested"]
    capture_kind: str | None = None
    offered: bool
    reasons: list[str] = Field(default_factory=list)
    reason_texts: list[str] = Field(default_factory=list)
    participants: list[ParticipantConsentOut] = Field(default_factory=list)
    jurisdiction: str | None = None
    recording_rule: str | None = None
    uploaded_at: datetime | None = None
    unknown_speakers: list[str] = Field(default_factory=list)
    failure_code: str | None = None
    run_id: uuid.UUID | None = None
    has_outputs: bool = False
    has_draft: bool = False
    can_manage: bool = False
    allow_recording_upload: bool = True
    allow_transcript_upload: bool = True


class ConsentRequestsOut(BaseModel):
    status: CaptureStatusOut
    sent: list[str]


class UploadOut(BaseModel):
    status: CaptureStatusOut
    transcript_id: uuid.UUID | None = None
    needs_transcript: bool = False


class SpeakerIn(_In):
    candidate_speaker: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]


class RecordingAckOut(BaseModel):
    acked: bool
    acked_at: datetime | None = None
    statement: str
    statement_version: str


class EvidenceQuote(BaseModel):
    segment_seq: int
    start_ms: int | None = None
    quote: str


class CompetencySummaryOut(BaseModel):
    competency_key: str
    label: str
    summary: str
    evidence: list[EvidenceQuote] = Field(default_factory=list)


class QaItemOut(BaseModel):
    asked_text: str
    kit_question_key: str | None = None
    kit_question_text: str | None = None
    match_confidence: float | None = None
    response_quote: str
    segment_seqs: list[int] = Field(default_factory=list)
    start_ms: int | None = None


class OutputsOut(BaseModel):
    capture_id: uuid.UUID
    interview_id: uuid.UUID
    candidate_id: uuid.UUID
    status: str
    summary_by_competency: list[CompetencySummaryOut]
    qa_items: list[QaItemOut]
    overall_summary: str | None
    ai_generated: bool = True
    skill_suggestion_count: int = 0
    created_at: datetime


class DraftRatingOut(BaseModel):
    competency_key: str
    label: str | None = None
    suggested_rating: int
    rationale: str
    evidence: list[EvidenceQuote] = Field(default_factory=list)


class DraftAnswerOut(BaseModel):
    question_key: str
    note_suggestion: str


class DraftOut(BaseModel):
    id: uuid.UUID
    interview_id: uuid.UUID
    participant_id: uuid.UUID
    ratings: list[DraftRatingOut]
    answers: list[DraftAnswerOut]
    recommendation: None = None
    status: str
    rating_scale: int
    show_suggestions_by_default: bool
    opened_at: datetime | None = None
    ai_generated: bool = True


class AssistantSettingsOut(BaseModel):
    show_suggestions_by_default: bool
    allow_recording_upload: bool
    allow_transcript_upload: bool
    consent_link_days: int
    max_upload_mb: int


class AssistantSettingsIn(_In):
    show_suggestions_by_default: bool | None = None
    allow_recording_upload: bool | None = None
    allow_transcript_upload: bool | None = None
    consent_link_days: int | None = Field(default=None, ge=1, le=30)
    max_upload_mb: int | None = Field(default=None, ge=1, le=200)


class RerunOut(BaseModel):
    run_id: uuid.UUID | None
    status: str
    reason: str | None = None


def as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


__all__ = [
    "AssistantSettingsIn",
    "AssistantSettingsOut",
    "CaptureStatusOut",
    "CompetencySummaryOut",
    "ConsentRequestsOut",
    "DraftAnswerOut",
    "DraftOut",
    "DraftRatingOut",
    "EvidenceQuote",
    "OutputsOut",
    "ParticipantConsentOut",
    "QaItemOut",
    "RecordingAckOut",
    "RerunOut",
    "SpeakerIn",
    "UploadOut",
]
