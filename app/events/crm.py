# ruff: noqa: E501
"""Sales CRM: leads and opportunities (Epic 15).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid

__all__ = [
    "LeadAssigned",
    "LeadConverted",
    "LeadCreated",
    "LeadStatusChanged",
    "OpportunityAssigned",
    "OpportunityCreated",
    "OpportunityLost",
    "OpportunityStageChanged",
    "OpportunityWon",
    "OpportunityWonHandoff",
]


@dataclasses.dataclass(frozen=True, slots=True)
class LeadStatusChanged:
    tenant_id: uuid.UUID
    lead_id: uuid.UUID
    from_status: str
    to_status: str


@dataclasses.dataclass(frozen=True, slots=True)
class OpportunityStageChanged:
    tenant_id: uuid.UUID
    opportunity_id: uuid.UUID
    from_stage: str
    to_stage: str


@dataclasses.dataclass(frozen=True, slots=True)
class OpportunityWon:
    tenant_id: uuid.UUID
    opportunity_id: uuid.UUID
    client_id: uuid.UUID


# ---------------------------------------------------------------- Epic 15 domain-local events (not in the frozen
# contract catalogue; ids only, no PII).
@dataclasses.dataclass(frozen=True, slots=True)
class LeadCreated:
    tenant_id: uuid.UUID
    lead_id: uuid.UUID
    owner_id: uuid.UUID
    actor_kind: str


@dataclasses.dataclass(frozen=True, slots=True)
class LeadConverted:
    tenant_id: uuid.UUID
    lead_id: uuid.UUID
    opportunity_id: uuid.UUID
    client_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class LeadAssigned:
    tenant_id: uuid.UUID
    lead_id: uuid.UUID
    owner_id: uuid.UUID
    actor_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class OpportunityCreated:
    tenant_id: uuid.UUID
    opportunity_id: uuid.UUID
    client_id: uuid.UUID
    owner_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class OpportunityAssigned:
    tenant_id: uuid.UUID
    opportunity_id: uuid.UUID
    owner_id: uuid.UUID
    actor_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class OpportunityLost:
    tenant_id: uuid.UUID
    opportunity_id: uuid.UUID
    reason_code: str


@dataclasses.dataclass(frozen=True, slots=True)
class OpportunityWonHandoff:
    """Emitted with ``OpportunityWon`` for the Account Manager notification (carries the AM id)."""

    tenant_id: uuid.UUID
    opportunity_id: uuid.UUID
    account_manager_id: uuid.UUID
    actor_id: uuid.UUID | None
