"""Interview capture events (Epic 53 contract layer, spec 0.4). Emitted by Epic 60.

Frozen, slotted dataclasses; ``tenant_id`` first; ids and scalars only; dispatched after commit. Pinned by
``tests/contracts/test_events_catalogue.py``.
"""

import dataclasses
import uuid

__all__ = ["InterviewCaptureReady"]


@dataclasses.dataclass(frozen=True, slots=True)
class InterviewCaptureReady:
    tenant_id: uuid.UUID
    capture_id: uuid.UUID
    interview_id: uuid.UUID
    candidate_id: uuid.UUID
    transcript_id: uuid.UUID | None
