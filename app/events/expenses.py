"""Expense claims (Epic 27).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid

__all__ = ["ExpenseClaimDecided", "ExpenseClaimSubmitted"]


@dataclasses.dataclass(frozen=True, slots=True)
class ExpenseClaimSubmitted:
    tenant_id: uuid.UUID
    claim_id: uuid.UUID
    placement_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class ExpenseClaimDecided:
    tenant_id: uuid.UUID
    claim_id: uuid.UUID
    placement_id: uuid.UUID
    outcome: str
