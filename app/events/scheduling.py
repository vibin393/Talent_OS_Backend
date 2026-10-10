"""Scheduling agent events (Epic 53 contract layer, spec 0.4). Emitted by Epic 59; resumes waiting runs
(``slot_pick``).

Frozen, slotted dataclasses; ``tenant_id`` first; ids and scalars only; dispatched after commit. Pinned by
``tests/contracts/test_events_catalogue.py``.
"""

import dataclasses
import uuid
from datetime import datetime

__all__ = ["SlotPicked"]


@dataclasses.dataclass(frozen=True, slots=True)
class SlotPicked:
    tenant_id: uuid.UUID
    proposal_ref: uuid.UUID
    interview_id: uuid.UUID | None
    application_id: uuid.UUID
    candidate_id: uuid.UUID
    slot_start: datetime
    slot_end: datetime
    run_id: uuid.UUID | None
