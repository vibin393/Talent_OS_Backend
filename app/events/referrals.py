"""Referral events (Epic 42).

Epic 36 contract layer (AD-36/AD-39): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII,
money as decimal ``str``); dispatched after commit. The events below are frozen
(``tests/contracts/test_events_catalogue.py``); the owner may add domain-local events to this file.
"""

import dataclasses
import uuid

__all__ = ["ReferralHired"]


@dataclasses.dataclass(frozen=True, slots=True)
class ReferralHired:
    tenant_id: uuid.UUID
    referral_id: uuid.UUID
    candidate_id: uuid.UUID
    application_id: uuid.UUID
    placement_id: uuid.UUID | None
    referrer_kind: str  # user | worker
    referrer_id: uuid.UUID
