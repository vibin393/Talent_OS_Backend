"""Domain-local bench events (Epic 24 A7). ``BenchEntryStatusChanged`` is the foundation class in
``app.events.placements``. Ids / numbers only; dispatched after commit."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class BenchEntryCreated:
    tenant_id: uuid.UUID
    bench_entry_id: uuid.UUID
    candidate_id: uuid.UUID
    origin: str


@dataclasses.dataclass(frozen=True, slots=True)
class BenchStrongMatchFound:
    tenant_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    overall: str


@dataclasses.dataclass(frozen=True, slots=True)
class BenchMatchesReviewed:
    tenant_id: uuid.UUID
    demand_id: uuid.UUID
