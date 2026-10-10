# ruff: noqa: E501
"""Transcript DTOs (Epic 53, AD-66, spec 0.13). No router in Epic 53: Epics 57 / 58 / 60 expose transcripts."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SourceKind = Literal["screening_conversation", "voice_session", "interview_capture", "upload"]
TranscriptStatus = Literal["pending", "ready", "failed", "erased"]
SpeakerRole = Literal["agent", "candidate", "interviewer", "recruiter", "unknown"]

MAX_SEGMENT_CHARS = 8000  # mirrors ``app.models.transcripts.MAX_SEGMENT_CHARS`` (DB CHECK)


class TranscriptSegmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    speaker_role: SpeakerRole = "unknown"
    speaker_ref: uuid.UUID | None = None
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    text: str = Field(min_length=1, max_length=MAX_SEGMENT_CHARS)

    @model_validator(mode="after")
    def _times(self) -> "TranscriptSegmentIn":
        if self.start_ms is not None and self.end_ms is not None and self.end_ms < self.start_ms:
            msg = "end_ms must not be before start_ms"
            raise ValueError(msg)
        if not self.text.strip():
            msg = "text must not be blank"
            raise ValueError(msg)
        return self


class TranscriptSegmentOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int
    speaker_role: SpeakerRole
    speaker_ref: uuid.UUID | None
    start_ms: int | None
    end_ms: int | None
    text: str


class TranscriptOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    source_kind: SourceKind
    source_id: uuid.UUID | None
    candidate_id: uuid.UUID | None
    application_id: uuid.UUID | None
    interview_id: uuid.UUID | None
    language: str | None
    status: TranscriptStatus
    segment_count: int
    file_id: uuid.UUID | None
    created_by_kind: str
    created_by_id: uuid.UUID | None
    attested_by: uuid.UUID | None
    attested_at: datetime | None
    erased_at: datetime | None
    created_at: datetime
    segments: tuple[TranscriptSegmentOut, ...] = ()


__all__ = [
    "MAX_SEGMENT_CHARS",
    "SourceKind",
    "SpeakerRole",
    "TranscriptOut",
    "TranscriptSegmentIn",
    "TranscriptSegmentOut",
    "TranscriptStatus",
]
