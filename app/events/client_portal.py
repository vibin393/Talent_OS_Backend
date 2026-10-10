"""Client Portal domain events (Epic 18; dispatched after commit). Ids only, no PII."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class ClientDemandRequested:
    tenant_id: uuid.UUID
    demand_id: uuid.UUID
    client_id: uuid.UUID
    client_user_id: uuid.UUID
    owner_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class ClientSlotsProposed:
    tenant_id: uuid.UUID
    proposal_id: uuid.UUID
    application_id: uuid.UUID
    task_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class ClientSlotChosen:
    tenant_id: uuid.UUID
    proposal_id: uuid.UUID
    application_id: uuid.UUID
    chosen_index: int
    task_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class ClientDelegationCreated:
    tenant_id: uuid.UUID
    delegation_id: uuid.UUID
    client_user_id: uuid.UUID
    delegate_client_user_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class CompliancePackPublished:
    tenant_id: uuid.UUID
    pack_id: uuid.UUID
    client_id: uuid.UUID
