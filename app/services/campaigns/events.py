# ruff: file-ignore[line-too-long]
"""Domain-local events of the ``campaigns`` domain (Epic 39).

They live here, not in ``app/events/campaigns.py``: that module is frozen by the Epic 36 contract snapshot
(``tests/contracts/test_events_catalogue.py`` allows only ``CampaignMessageEngaged`` there). Ids only, after commit.
"""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class CampaignStartRequested:
    tenant_id: uuid.UUID
    campaign_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class MarketingBatchReady:
    tenant_id: uuid.UUID
    message_ids: tuple[uuid.UUID, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateDormant:
    """Workflow trigger ``campaigns.candidate_dormant``: last activity crossed ``months`` today."""

    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
    months: int


@dataclasses.dataclass(frozen=True, slots=True)
class SilverMedalistMatched:
    """Workflow trigger ``campaigns.silver_medalist_matched`` (Epic 38 "Silver Medalists" pool x stored matches)."""

    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
    demand_id: uuid.UUID
    score: int
