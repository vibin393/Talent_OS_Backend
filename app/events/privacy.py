"""Privacy domain events (Epic 12; dispatched after commit, see ``app.core.events``)."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class ConsentGranted:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
    purpose: str
    consent_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class ConsentWithdrawn:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
    purpose: str


@dataclasses.dataclass(frozen=True, slots=True)
class AIProcessingBasisChanged:
    tenant_id: uuid.UUID
    basis: str


@dataclasses.dataclass(frozen=True, slots=True)
class ErasureRequested:
    """An erasure run was created; the handler enqueues ``run_erasure(tenant_id, run_id)``."""

    tenant_id: uuid.UUID
    run_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class SubjectExportRequested:
    tenant_id: uuid.UUID
    dsr_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class RetentionExecutionRequested:
    tenant_id: uuid.UUID
    run_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class DsrCompleted:
    tenant_id: uuid.UUID
    dsr_id: uuid.UUID
    kind: str


# ================================================================ Epic 35 (retention classes, AD-34)
@dataclasses.dataclass(frozen=True, slots=True)
class RetentionExpiryPreviewed:
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    class_key: str
    total: int


@dataclasses.dataclass(frozen=True, slots=True)
class RetentionExpired:
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    class_key: str
    table: str
    count: int
