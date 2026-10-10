# ruff: noqa: E501
"""Domain-local BGV events (Epic 20; not in the frozen foundation catalogue). Ids only, no PII; dispatched after
commit. They drive the ``bgv_*`` / ``reference_requested`` notification types."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class BgvDocumentsRequested:
    tenant_id: uuid.UUID
    case_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class BgvCheckOverdue:
    tenant_id: uuid.UUID
    check_id: uuid.UUID
    case_id: uuid.UUID
    recipient_ids: tuple[uuid.UUID, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class BgvConsentWithdrawnNotice:
    tenant_id: uuid.UUID
    case_id: uuid.UUID
    recipient_ids: tuple[uuid.UUID, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class ReferenceRequested:
    tenant_id: uuid.UUID
    reference_check_id: uuid.UUID
    candidate_id: uuid.UUID
