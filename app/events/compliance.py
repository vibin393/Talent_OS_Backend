"""Compliance requirements and credentials (Epic 21).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid

__all__ = [
    "ComplianceRequirementsEvaluated",
    "CredentialRecorded",
    "CredentialStatusChanged",
    "CredentialVerified",
    "PlacementComplianceChanged",
]


@dataclasses.dataclass(frozen=True, slots=True)
class CredentialStatusChanged:
    tenant_id: uuid.UUID
    credential_id: uuid.UUID
    candidate_id: uuid.UUID
    credential_type: str
    from_status: str
    to_status: str
    mandatory: bool
    mandatory_for_placement_ids: tuple[uuid.UUID, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class PlacementComplianceChanged:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    compliant: bool
    blocking: bool


@dataclasses.dataclass(frozen=True, slots=True)
class ComplianceRequirementsEvaluated:
    tenant_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    candidate_id: uuid.UUID
    placement_id: uuid.UUID | None
    bgv_package_ids: tuple[uuid.UUID, ...]
    credential_type_keys: tuple[str, ...]
    document_categories: tuple[str, ...]
    policy_template_ids: tuple[uuid.UUID, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class CredentialRecorded:
    tenant_id: uuid.UUID
    credential_id: uuid.UUID
    candidate_id: uuid.UUID
    type_key: str


@dataclasses.dataclass(frozen=True, slots=True)
class CredentialVerified:
    tenant_id: uuid.UUID
    credential_id: uuid.UUID
    candidate_id: uuid.UUID
    type_key: str
    verification_status: str
