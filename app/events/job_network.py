"""Domain events of the Cross-tenant Job Network (Epic 75, spec A7). Tenant-scoped; no applicant data."""

import dataclasses
import uuid
from datetime import UTC, datetime


@dataclasses.dataclass(frozen=True, slots=True)
class NetworkListingChanged:
    tenant_id: uuid.UUID
    demand_id: uuid.UUID
    listing_id: uuid.UUID
    status: str
    reason: str | None = None
    occurred_at: datetime = dataclasses.field(default_factory=lambda: datetime.now(UTC))


@dataclasses.dataclass(frozen=True, slots=True)
class NetworkMembershipChanged:
    tenant_id: uuid.UUID
    status: str
    verification_status: str
    actor_kind: str | None = None
    occurred_at: datetime = dataclasses.field(default_factory=lambda: datetime.now(UTC))


__all__ = ["NetworkListingChanged", "NetworkMembershipChanged"]
