"""Provider connector jobs (Epic 13, AD-29).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid

__all__ = ["ConnectorJobFailed", "ConnectorJobQueued"]


@dataclasses.dataclass(frozen=True, slots=True)
class ConnectorJobQueued:
    tenant_id: uuid.UUID
    job_id: uuid.UUID
    category: str
    op: str


@dataclasses.dataclass(frozen=True, slots=True)
class ConnectorJobFailed:
    tenant_id: uuid.UUID
    job_id: uuid.UUID
    category: str
    op: str
    subject_kind: str
    subject_id: uuid.UUID
