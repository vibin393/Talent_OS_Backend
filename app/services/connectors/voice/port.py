# ruff: noqa: E501
"""``voice`` port (FR-293 "call session + transcription"; Epic 53 contract 0.13, owned by Epic 58). ``manual``
asks the user to call outside Talent OS; ``fake`` scripts a deterministic session. Every adapter also implements
the generic ``ConnectorPort``. ``TranscriptionProvider`` is a capability: only the fake implements it in Phase 4.
Signatures are frozen.
"""

import dataclasses
import uuid
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

__all__ = [
    "CallEventDTO",
    "CallSessionDTO",
    "CallSessionRequestDTO",
    "RecordingDTO",
    "SpeakerRole",
    "TranscriptDTO",
    "TranscriptSegmentDTO",
    "TranscriptionProvider",
    "VoiceProvider",
]

SpeakerRole = Literal["agent", "candidate", "interviewer", "recruiter", "unknown"]


@dataclasses.dataclass(frozen=True, slots=True)
class CallSessionRequestDTO:
    tenant_id: uuid.UUID
    voice_session_id: uuid.UUID
    to_e164: str
    scheduled_at: datetime | None
    record: bool
    language: str | None
    max_duration_s: int
    callback_url: str | None
    script_ref: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class CallSessionDTO:
    external_ref: str
    status: Literal["scheduled", "started", "ended", "manual"]
    recording_ref: str | None = None
    transcript_ref: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class RecordingDTO:
    data: bytes
    media_type: str
    duration_s: int


@dataclasses.dataclass(frozen=True, slots=True)
class TranscriptSegmentDTO:
    speaker: SpeakerRole
    start_ms: int | None
    end_ms: int | None
    text: str


@dataclasses.dataclass(frozen=True, slots=True)
class TranscriptDTO:
    segments: tuple[TranscriptSegmentDTO, ...]
    language: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class CallEventDTO:
    external_ref: str
    kind: Literal[
        "scheduled", "started", "ended", "no_answer", "failed", "recording_ready", "transcript_ready"
    ]
    occurred_at: datetime


@runtime_checkable
class VoiceProvider(Protocol):
    category: str
    provider_key: str

    async def schedule_call(self, req: CallSessionRequestDTO) -> CallSessionDTO: ...

    async def start_call(self, external_ref: str) -> CallSessionDTO: ...

    async def end_call(self, external_ref: str) -> CallSessionDTO: ...

    async def fetch_recording(self, recording_ref: str) -> RecordingDTO | None: ...

    async def fetch_transcript(self, transcript_ref: str) -> TranscriptDTO | None: ...


@runtime_checkable
class TranscriptionProvider(Protocol):
    async def transcribe(self, audio: bytes, media_type: str, language_hint: str | None) -> TranscriptDTO: ...
