"""Assessment events (Epic 40).

Epic 36 contract layer (AD-36/AD-39): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII,
money as decimal ``str``); dispatched after commit. The events below are frozen
(``tests/contracts/test_events_catalogue.py``); the owner may add domain-local events to this file.
"""

import dataclasses
import uuid

__all__ = ["AssessmentInvitationDecided", "AssessmentScored"]


@dataclasses.dataclass(frozen=True, slots=True)
class AssessmentScored:
    tenant_id: uuid.UUID
    invitation_id: uuid.UUID
    assessment_id: uuid.UUID
    candidate_id: uuid.UUID
    application_id: uuid.UUID | None
    score: str | None
    skill_scores: tuple[tuple[str, int], ...]  # (skill_key, proficiency 1..5)
    source: str  # builtin | provider


@dataclasses.dataclass(frozen=True, slots=True)
class AssessmentInvitationDecided:
    tenant_id: uuid.UUID
    invitation_id: uuid.UUID
    assessment_id: uuid.UUID
    candidate_id: uuid.UUID
    application_id: uuid.UUID | None
    context_kind: str
    context_id: uuid.UUID | None
    outcome: str  # passed | failed | cancelled | expired
    gating: bool
    actor_kind: str
