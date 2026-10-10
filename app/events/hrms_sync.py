"""HRMS sync events (Epic 71, spec A10). Frozen, slotted dataclasses; ``tenant_id`` first; ids and statuses only."""

import dataclasses
import uuid

__all__ = ["HrmsSyncStatusChanged"]


@dataclasses.dataclass(frozen=True, slots=True)
class HrmsSyncStatusChanged:
    tenant_id: uuid.UUID
    record_id: uuid.UUID
    placement_id: uuid.UUID
    status: str
