"""Platform events (Phase 2 Epic 33, AD-38): custom Career Portal domains. Ids and status codes only."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class CustomDomainVerified:
    tenant_id: uuid.UUID
    domain_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class CustomDomainRemoved:
    tenant_id: uuid.UUID
    domain_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class CustomDomainFailed:
    tenant_id: uuid.UUID
    domain_id: uuid.UUID
