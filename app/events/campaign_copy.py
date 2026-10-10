"""Domain-local events of ``campaign_copy`` (Epic 78, FR-401..FR-403). Ids and scalars only.

Dispatched after commit."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class CampaignCopyGenerated:
    tenant_id: uuid.UUID
    request_id: uuid.UUID
    campaign_id: uuid.UUID | None
    sequence_step_id: uuid.UUID | None
    drafts: int


@dataclasses.dataclass(frozen=True, slots=True)
class CampaignCopyApproved:
    tenant_id: uuid.UUID
    draft_id: uuid.UUID
    campaign_id: uuid.UUID | None
    approved_by: uuid.UUID
    rubber_stamp: bool


__all__ = ["CampaignCopyApproved", "CampaignCopyGenerated"]
