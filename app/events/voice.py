"""Voice session events (Epic 53 contract layer, spec 0.4). Emitted by Epic 58.

Frozen, slotted dataclasses; ``tenant_id`` first; ids and scalars only; dispatched after commit. Pinned by
``tests/contracts/test_events_catalogue.py``.
"""

import dataclasses
import uuid

__all__ = ["VoiceSessionReady"]


@dataclasses.dataclass(frozen=True, slots=True)
class VoiceSessionReady:
    tenant_id: uuid.UUID
    voice_session_id: uuid.UUID
    candidate_id: uuid.UUID
    application_id: uuid.UUID | None
    transcript_id: uuid.UUID | None
    recording_file_id: uuid.UUID | None
