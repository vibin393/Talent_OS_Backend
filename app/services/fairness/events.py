"""Domain-local events of ``fairness`` (Epic 49; the ``app/events`` catalogue is frozen). Ids only."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class RunRequested:
    tenant_id: uuid.UUID
    run_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class AlternativeRequested:
    tenant_id: uuid.UUID
    request_id: uuid.UUID
    demand_id: uuid.UUID
