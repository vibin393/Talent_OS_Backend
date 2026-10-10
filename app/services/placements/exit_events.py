"""Epic 31 domain-local events (not part of the frozen foundation catalogue). Ids and enums only, no PII;
dispatched after commit. They drive notifications and workflow triggers."""

import dataclasses
import uuid
from datetime import date


@dataclasses.dataclass(frozen=True, slots=True)
class PlacementEndingSoon:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    end_date: date
    offset_days: int
    days_left: int
    recipient_ids: tuple[uuid.UUID, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class OffboardingItemCompleted:
    tenant_id: uuid.UUID
    case_id: uuid.UUID
    placement_id: uuid.UUID
    item_id: uuid.UUID
    kind: str
    status: str
    completed_by_kind: str


@dataclasses.dataclass(frozen=True, slots=True)
class OffboardingItemOverdue:
    tenant_id: uuid.UUID
    case_id: uuid.UUID
    placement_id: uuid.UUID
    item_id: uuid.UUID
    owner_id: uuid.UUID | None
