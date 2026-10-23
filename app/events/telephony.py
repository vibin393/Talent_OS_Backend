# ruff: file-ignore[line-too-long]
"""Telephony events (Epic 76, spec A9). Frozen, slotted dataclasses; ``tenant_id`` first; ids and scalars only (no
phone numbers, no recording refs); dispatched after commit."""

import dataclasses
import uuid

__all__ = [
    "SttJobQueued",
    "TelephonyCostThreshold",
    "TelephonyProviderDeleteQueued",
    "TelephonyRecordingReady",
    "VoiceCallEnded",
    "VoiceCallPlaced",
]


@dataclasses.dataclass(frozen=True, slots=True)
class VoiceCallPlaced:
    tenant_id: uuid.UUID
    call_id: uuid.UUID
    voice_session_id: uuid.UUID
    candidate_id: uuid.UUID | None
    placed_by: uuid.UUID
    provider_key: str
    recorded: bool


@dataclasses.dataclass(frozen=True, slots=True)
class VoiceCallEnded:
    tenant_id: uuid.UUID
    call_id: uuid.UUID
    voice_session_id: uuid.UUID
    candidate_id: uuid.UUID | None
    application_id: uuid.UUID | None
    outcome: str
    duration_s: int | None
    billable_minutes: int | None


@dataclasses.dataclass(frozen=True, slots=True)
class TelephonyCostThreshold:
    tenant_id: uuid.UUID
    day: str
    pct: int


# Internal (after-commit task kicks; not public).
@dataclasses.dataclass(frozen=True, slots=True)
class TelephonyRecordingReady:
    tenant_id: uuid.UUID
    call_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class TelephonyProviderDeleteQueued:
    tenant_id: uuid.UUID
    call_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class SttJobQueued:
    tenant_id: uuid.UUID
    job_id: uuid.UUID
