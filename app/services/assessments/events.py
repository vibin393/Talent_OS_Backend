"""Domain-local (not frozen) after-commit events of the ``assessments`` domain (Epic 40).

They feed the notification types and the workflow triggers ``assessments.invitation_status_changed`` /
``assessments.scored``. The frozen cross-domain events (``AssessmentScored``, ``AssessmentInvitationDecided``)
live in ``app/events/assessments.py``.
"""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class AssessmentInvitationStatusChanged:
    tenant_id: uuid.UUID
    invitation_id: uuid.UUID
    assessment_id: uuid.UUID
    candidate_id: uuid.UUID | None
    application_id: uuid.UUID | None
    from_status: str | None
    to_status: str
    actor_kind: str
    inviter_id: uuid.UUID | None = None
    actor_id: uuid.UUID | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class AssessmentReminderDue:
    tenant_id: uuid.UUID
    invitation_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class AssessmentReviewNeeded:
    tenant_id: uuid.UUID
    invitation_id: uuid.UUID
    inviter_id: uuid.UUID | None
    pending: int


@dataclasses.dataclass(frozen=True, slots=True)
class AssessmentResultReady:
    """Workflow trigger ``assessments.scored`` (domain-local twin of the frozen ``AssessmentScored``)."""

    tenant_id: uuid.UUID
    invitation_id: uuid.UUID
    application_id: uuid.UUID | None
    candidate_id: uuid.UUID | None
    score: str | None
    source: str


@dataclasses.dataclass(frozen=True, slots=True)
class AssessmentProviderInviteQueued:
    """After commit -> ``assessments.provider_invite`` (queue ``connectors``)."""

    tenant_id: uuid.UUID
    invitation_id: uuid.UUID
    job_id: uuid.UUID
