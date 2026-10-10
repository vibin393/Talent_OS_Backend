"""Immigration case events (Epic 46). ``PermitRecorded`` feeds the P2 compliance credential handler.

Epic 36 contract layer (AD-36/AD-39): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII,
money as decimal ``str``); dispatched after commit. The events below are frozen
(``tests/contracts/test_events_catalogue.py``); the owner may add domain-local events to this file.
"""

import dataclasses
import uuid
from datetime import date

__all__ = ["PermitRecorded"]


@dataclasses.dataclass(frozen=True, slots=True)
class PermitRecorded:
    tenant_id: uuid.UUID
    permit_id: uuid.UUID
    immigration_case_id: uuid.UUID
    candidate_id: uuid.UUID
    placement_id: uuid.UUID | None
    country: str
    permit_type: str
    valid_from: date
    valid_to: date | None
    credential_type_key: str
