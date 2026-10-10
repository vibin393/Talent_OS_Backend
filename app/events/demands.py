"""Demand domain events (dispatched after commit, see ``app.core.events``). Ids and codes only, no PII."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class DemandCreated:
    tenant_id: uuid.UUID
    demand_id: uuid.UUID
    actor_kind: str


@dataclasses.dataclass(frozen=True, slots=True)
class DemandSubmitted:
    tenant_id: uuid.UUID
    demand_id: uuid.UUID
    approval_request_id: uuid.UUID | None
    auto_approved: bool


@dataclasses.dataclass(frozen=True, slots=True)
class DemandOpened:
    tenant_id: uuid.UUID
    demand_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class DemandStatusChanged:
    tenant_id: uuid.UUID
    demand_id: uuid.UUID
    from_status: str
    to_status: str
    actor_kind: str
    reason_code: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class DemandAssigned:
    tenant_id: uuid.UUID
    demand_id: uuid.UUID
    added_user_ids: tuple[uuid.UUID, ...]
    expanded_team_member_ids: tuple[uuid.UUID, ...]
    primary_user_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class DemandPublished:
    tenant_id: uuid.UUID
    demand_id: uuid.UUID
    jd_version_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class DemandUnpublished:
    tenant_id: uuid.UUID
    demand_id: uuid.UUID
    actor_kind: str


@dataclasses.dataclass(frozen=True, slots=True)
class DemandSlaStatusChanged:
    tenant_id: uuid.UUID
    demand_id: uuid.UUID
    milestone: str
    status: str
    recipient_ids: tuple[uuid.UUID, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class DemandRequirementsChanged:
    """Matching-relevant fields or skills of a demand changed (Epic 5; emitted by
    ``app.services.demands.matching_port``'s flush listener)."""

    tenant_id: uuid.UUID
    demand_id: uuid.UUID
