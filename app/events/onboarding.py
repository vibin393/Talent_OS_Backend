"""Digital onboarding (Epic 23).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid

__all__ = [
    "OnboardingCaseCancelled",
    "OnboardingCaseCompleted",
    "OnboardingCaseCreated",
    "OnboardingProgressChanged",
    "OnboardingTaskOverdue",
    "OnboardingTaskReturned",
    "PolicyAcknowledged",
]


@dataclasses.dataclass(frozen=True, slots=True)
class OnboardingCaseCreated:
    tenant_id: uuid.UUID
    case_id: uuid.UUID
    placement_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class OnboardingProgressChanged:
    tenant_id: uuid.UUID
    case_id: uuid.UUID
    placement_id: uuid.UUID
    pct: int


@dataclasses.dataclass(frozen=True, slots=True)
class OnboardingCaseCompleted:
    tenant_id: uuid.UUID
    case_id: uuid.UUID
    placement_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class OnboardingCaseCancelled:
    tenant_id: uuid.UUID
    case_id: uuid.UUID
    placement_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class OnboardingTaskReturned:
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    case_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class OnboardingTaskOverdue:
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    case_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class PolicyAcknowledged:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
    template_id: uuid.UUID
    template_version_id: uuid.UUID
