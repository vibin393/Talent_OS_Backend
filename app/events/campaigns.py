"""Recruitment marketing campaign events (Epic 39).

Epic 36 contract layer (AD-36/AD-39): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII,
money as decimal ``str``); dispatched after commit. The events below are frozen
(``tests/contracts/test_events_catalogue.py``); the owner may add domain-local events to this file.
"""

import dataclasses
import uuid
from datetime import datetime

__all__ = ["CampaignMessageEngaged"]


@dataclasses.dataclass(frozen=True, slots=True)
class CampaignMessageEngaged:
    tenant_id: uuid.UUID
    campaign_message_id: uuid.UUID
    campaign_id: uuid.UUID | None
    enrollment_id: uuid.UUID | None
    candidate_id: uuid.UUID
    kind: str  # delivered | opened | clicked | replied | unsubscribed | bounced | complained
    occurred_at: datetime
