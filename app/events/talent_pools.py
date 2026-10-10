"""Talent pool events (Epic 38). Members are read via ``pools_service.member_ids(..., change=, refresh_id=)``.

Epic 36 contract layer (AD-36/AD-39): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII,
money as decimal ``str``); dispatched after commit. The events below are frozen
(``tests/contracts/test_events_catalogue.py``); the owner may add domain-local events to this file.
"""

import dataclasses
import uuid
from datetime import datetime

__all__ = ["PoolMembershipChanged"]


@dataclasses.dataclass(frozen=True, slots=True)
class PoolMembershipChanged:
    tenant_id: uuid.UUID
    pool_id: uuid.UUID
    refresh_id: uuid.UUID | None
    added_count: int
    removed_count: int
    changed_at: datetime
