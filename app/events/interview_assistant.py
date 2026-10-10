# ruff: noqa: E501
"""Interview Assistant domain-local events (Epic 60). ``DraftRatingsDecided`` records how an interviewer used the
draft's suggested ratings when submitting their own scorecard (FR-323 selection data for Epic 65).

Frozen, slotted dataclasses; ``tenant_id`` first; ids and scalars only; dispatched after commit.
"""

import dataclasses
import uuid

__all__ = ["DraftRatingsDecided"]


@dataclasses.dataclass(frozen=True, slots=True)
class DraftRatingsDecided:
    tenant_id: uuid.UUID
    draft_id: uuid.UUID
    interview_id: uuid.UUID
    candidate_id: uuid.UUID
    accepted: int
    changed: int
    ignored: int
