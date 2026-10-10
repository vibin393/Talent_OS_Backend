"""Domain-local events of the report builder (Epic 47; dispatched after commit, ids only)."""

import dataclasses
import uuid

__all__ = ["ReportExportQueued"]


@dataclasses.dataclass(frozen=True, slots=True)
class ReportExportQueued:
    """A large builder export was recorded ``queued``; the handler enqueues ``reports.run_export``."""

    tenant_id: uuid.UUID
    run_id: uuid.UUID
