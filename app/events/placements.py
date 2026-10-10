"""Placements, rate segments, start gates, extensions, offboarding and bench (Epics 19, 24, 31).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid
from datetime import date

__all__ = [
    "BankDetailsChangeDecided",
    "BenchEntryStatusChanged",
    "ExtensionConfirmed",
    "ExtensionStatusChanged",
    "OffboardingCaseStatusChanged",
    "PlacementActivated",
    "PlacementConverted",
    "PlacementCreated",
    "PlacementDropped",
    "PlacementEnded",
    "PlacementStatusChanged",
    "RateSegmentAdded",
    "StartGateStatusChanged",
]


@dataclasses.dataclass(frozen=True, slots=True)
class PlacementCreated:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    candidate_id: uuid.UUID
    client_id: uuid.UUID
    vendor_id: uuid.UUID | None
    legal_entity_id: uuid.UUID
    offer_id: uuid.UUID | None
    demand_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class PlacementStatusChanged:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    from_status: str
    to_status: str
    reason: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class PlacementActivated:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    candidate_id: uuid.UUID
    actual_start_date: date


@dataclasses.dataclass(frozen=True, slots=True)
class PlacementEnded:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    candidate_id: uuid.UUID
    end_date: date
    reason: str | None
    worker_type: str
    bench_eligible: bool


@dataclasses.dataclass(frozen=True, slots=True)
class PlacementDropped:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    demand_id: uuid.UUID | None
    reason: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class RateSegmentAdded:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    rate_segment_id: uuid.UUID
    effective_from: date
    previous_segment_id: uuid.UUID | None
    superseded: tuple[tuple[uuid.UUID, date, date | None], ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class StartGateStatusChanged:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    gate_kind: str
    source_ref: str
    status: str


@dataclasses.dataclass(frozen=True, slots=True)
class ExtensionStatusChanged:
    tenant_id: uuid.UUID
    extension_id: uuid.UUID
    placement_id: uuid.UUID
    from_status: str
    to_status: str


@dataclasses.dataclass(frozen=True, slots=True)
class ExtensionConfirmed:
    tenant_id: uuid.UUID
    extension_id: uuid.UUID
    placement_id: uuid.UUID
    new_end_date: date
    rate_segment_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class OffboardingCaseStatusChanged:
    tenant_id: uuid.UUID
    case_id: uuid.UUID
    placement_id: uuid.UUID
    from_status: str
    to_status: str


@dataclasses.dataclass(frozen=True, slots=True)
class BenchEntryStatusChanged:
    tenant_id: uuid.UUID
    bench_entry_id: uuid.UUID
    candidate_id: uuid.UUID
    from_status: str
    to_status: str


@dataclasses.dataclass(frozen=True, slots=True)
class PlacementConverted:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    conversion_id: uuid.UUID
    client_id: uuid.UUID
    conversion_date: date
    fee_amount: str | None
    currency: str


@dataclasses.dataclass(frozen=True, slots=True)
class BankDetailsChangeDecided:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
    bank_account_id: uuid.UUID
    outcome: str
