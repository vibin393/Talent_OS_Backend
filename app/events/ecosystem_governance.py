"""Epic 79 (Phase 5 Governance) internal signals. The governance package reacts to foundation/privacy events
(``DsrCompleted``) and owns no public outbox event types of its own; these are internal-only signals."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class AccessReviewOpened:
    tenant_id: uuid.UUID
    review_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class AccessReviewAutoRevoked:
    tenant_id: uuid.UUID
    review_id: uuid.UUID
    item_id: uuid.UUID


__all__ = ["AccessReviewAutoRevoked", "AccessReviewOpened"]
