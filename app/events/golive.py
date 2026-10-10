"""Go-live import events (Epic 35 A8, FR-185; dispatched after commit). Ids and counts only."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class GoLiveImportValidated:
    """A go-live CSV was parsed and dry-validated through its owner function (status ``previewed``)."""

    tenant_id: uuid.UUID
    batch_id: uuid.UUID
    kind: str
    row_count: int
    error_count: int


@dataclasses.dataclass(frozen=True, slots=True)
class GoLiveImportApplied:
    """A previewed go-live batch was applied row by row (each row in its own savepoint)."""

    tenant_id: uuid.UUID
    batch_id: uuid.UUID
    kind: str
    applied_count: int
    failed_count: int
    skipped_count: int
