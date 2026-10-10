"""Vendors, releases and vendor submissions (Epic 17).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid

__all__ = [
    "VendorReleaseClosed",
    "VendorReleaseCreated",
    "VendorStatusChanged",
    "VendorSubmissionDecided",
    "VendorSubmissionReceived",
]


@dataclasses.dataclass(frozen=True, slots=True)
class VendorStatusChanged:
    tenant_id: uuid.UUID
    vendor_id: uuid.UUID
    from_status: str
    to_status: str


@dataclasses.dataclass(frozen=True, slots=True)
class VendorReleaseCreated:
    tenant_id: uuid.UUID
    release_id: uuid.UUID
    demand_id: uuid.UUID
    vendor_ids: tuple[uuid.UUID, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class VendorReleaseClosed:
    tenant_id: uuid.UUID
    release_id: uuid.UUID
    demand_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class VendorSubmissionReceived:
    tenant_id: uuid.UUID
    submission_id: uuid.UUID
    release_id: uuid.UUID
    vendor_id: uuid.UUID
    demand_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class VendorSubmissionDecided:
    tenant_id: uuid.UUID
    submission_id: uuid.UUID
    vendor_id: uuid.UUID
    outcome: str
    application_id: uuid.UUID | None
