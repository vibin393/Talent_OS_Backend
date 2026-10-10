"""Background verification and reference checks (Epic 20).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid
from typing import Literal

__all__ = ["BgvCaseAdjudicated", "BgvCaseOpened", "BgvCaseStatusChanged", "BgvDiscrepancyRaised"]


@dataclasses.dataclass(frozen=True, slots=True)
class BgvCaseOpened:
    tenant_id: uuid.UUID
    case_id: uuid.UUID
    candidate_id: uuid.UUID
    placement_id: uuid.UUID | None
    package_id: uuid.UUID | None
    required: bool


@dataclasses.dataclass(frozen=True, slots=True)
class BgvCaseStatusChanged:
    tenant_id: uuid.UUID
    case_id: uuid.UUID
    candidate_id: uuid.UUID
    placement_id: uuid.UUID | None
    from_status: str
    to_status: str


@dataclasses.dataclass(frozen=True, slots=True)
class BgvCaseAdjudicated:
    tenant_id: uuid.UUID
    case_id: uuid.UUID
    candidate_id: uuid.UUID
    placement_id: uuid.UUID | None
    outcome: Literal["clear", "failed"]
    with_exceptions: bool


@dataclasses.dataclass(frozen=True, slots=True)
class BgvDiscrepancyRaised:
    tenant_id: uuid.UUID
    discrepancy_id: uuid.UUID
    case_id: uuid.UUID
    severity: str
