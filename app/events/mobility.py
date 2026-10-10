"""Internal mobility events (Epic 41).

Epic 36 contract layer (AD-36/AD-39): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII,
money as decimal ``str``); dispatched after commit. The events below are frozen
(``tests/contracts/test_events_catalogue.py``); the owner may add domain-local events to this file.
"""

import dataclasses
import uuid

__all__ = ["InternalTransferConfirmed"]


@dataclasses.dataclass(frozen=True, slots=True)
class InternalTransferConfirmed:
    tenant_id: uuid.UUID
    application_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    employee_kind: str  # user | worker
    user_id: uuid.UUID | None
    from_placement_id: uuid.UUID | None
    new_placement_id: uuid.UUID | None
