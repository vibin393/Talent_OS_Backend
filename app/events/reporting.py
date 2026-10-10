"""Reporting events (Epic 11, dispatched after commit). Ids only."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class ExportQueued:
    """A large export was recorded ``queued``; the handler enqueues ``reporting.run_export``."""

    tenant_id: uuid.UUID
    export_id: uuid.UUID
    requested_by: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class ExportCompleted:
    """An async export finished and its file is downloadable (drives Epic 10's ``export_ready``)."""

    tenant_id: uuid.UUID
    export_id: uuid.UUID
    requested_by: uuid.UUID
    kind: str
    format: str
    row_count: int
