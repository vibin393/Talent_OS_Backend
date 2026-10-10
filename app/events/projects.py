"""Project / SOW delivery events (Epic 45).

Epic 36 contract layer (AD-36/AD-39): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII,
money as decimal ``str``); dispatched after commit. The events below are frozen
(``tests/contracts/test_events_catalogue.py``); the owner may add domain-local events to this file.
"""

import dataclasses
import uuid
from datetime import date

__all__ = [
    "DeliverableAccepted",
    "DeliverableRejected",
    "DeliverableSubmitted",
    "ProjectAssignmentEnded",
    "ProjectBudgetThresholdReached",
]


@dataclasses.dataclass(frozen=True, slots=True)
class DeliverableAccepted:
    tenant_id: uuid.UUID
    deliverable_id: uuid.UUID
    project_id: uuid.UUID
    client_id: uuid.UUID
    milestone_id: uuid.UUID | None
    actor_kind: str


@dataclasses.dataclass(frozen=True, slots=True)
class ProjectAssignmentEnded:
    tenant_id: uuid.UUID
    assignment_id: uuid.UUID
    project_id: uuid.UUID
    project_role_id: uuid.UUID
    placement_id: uuid.UUID
    candidate_id: uuid.UUID
    ended_on: date


# ---------------------------------------------------------------- Epic 45 domain-local events
@dataclasses.dataclass(frozen=True, slots=True)
class DeliverableSubmitted:
    tenant_id: uuid.UUID
    deliverable_id: uuid.UUID
    project_id: uuid.UUID
    client_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class DeliverableRejected:
    tenant_id: uuid.UUID
    deliverable_id: uuid.UUID
    project_id: uuid.UUID
    client_id: uuid.UUID
    actor_kind: str


@dataclasses.dataclass(frozen=True, slots=True)
class ProjectBudgetThresholdReached:
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    dimension: str
    threshold: int
    budget_version: int
