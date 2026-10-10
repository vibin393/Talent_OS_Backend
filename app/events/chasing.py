"""Domain-local events of ``chasing`` (Epic 62, FR-308). Ids and scalars only; dispatched after commit."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class ChaseEscalated:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
    placement_id: uuid.UUID | None
    reason: str
    task_id: uuid.UUID
    item_count: int


__all__ = ["ChaseEscalated"]
