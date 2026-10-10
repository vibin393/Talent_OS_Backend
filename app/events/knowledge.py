"""Domain-local events of ``knowledge`` (Epic 62, FR-310). Ids and scalars only; dispatched after commit."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class KnowledgeSourcePublished:
    tenant_id: uuid.UUID
    source_id: uuid.UUID
    version_no: int
    audience: str


@dataclasses.dataclass(frozen=True, slots=True)
class KnowledgeSourceUnpublished:
    tenant_id: uuid.UUID
    source_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class OnboardingQuestionAsked:
    tenant_id: uuid.UUID
    thread_id: uuid.UUID
    candidate_id: uuid.UUID
    message_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class OnboardingQuestionHandedOff:
    tenant_id: uuid.UUID
    thread_id: uuid.UUID
    candidate_id: uuid.UUID
    task_id: uuid.UUID
    topic: str | None
    reason: str


__all__ = [
    "KnowledgeSourcePublished",
    "KnowledgeSourceUnpublished",
    "OnboardingQuestionAsked",
    "OnboardingQuestionHandedOff",
]
