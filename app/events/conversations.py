"""Screening conversation events (Epic 53 contract layer, spec 0.4). Emitted by Epic 57.

Frozen, slotted dataclasses; ``tenant_id`` first; ids and scalars only; dispatched after commit. Pinned by
``tests/contracts/test_events_catalogue.py``.
"""

import dataclasses
import uuid

__all__ = ["ScreeningConversationCompleted"]


@dataclasses.dataclass(frozen=True, slots=True)
class ScreeningConversationCompleted:
    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    application_id: uuid.UUID
    candidate_id: uuid.UUID
    outcome: str  # completed | handed_off | expired | withdrawn
    transcript_id: uuid.UUID | None
    run_id: uuid.UUID | None
