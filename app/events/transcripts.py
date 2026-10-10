# ruff: noqa: E501
"""Transcript events (Epic 53, spec 0.4). Emitted by ``transcripts`` when a transcript becomes ``ready``;
consumed by Epics 57, 58 and 60.

Frozen, slotted dataclasses; ``tenant_id`` first; ids and scalars only (never transcript text); dispatched after
commit. Pinned by ``tests/contracts/test_events_catalogue.py``.
"""

import dataclasses
import uuid

__all__ = ["TranscriptReady"]


@dataclasses.dataclass(frozen=True, slots=True)
class TranscriptReady:
    tenant_id: uuid.UUID
    transcript_id: uuid.UUID
    source_kind: str
    source_id: uuid.UUID | None
    candidate_id: uuid.UUID | None
    application_id: uuid.UUID | None
    interview_id: uuid.UUID | None
