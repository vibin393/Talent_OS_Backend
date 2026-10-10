"""Interview domain events (Epic 7; dispatched after commit). Ids, enums and stage names only -- no PII.

Consumers: the interview invite / reminder tasks (``services.interviews.handlers``), Epic 10 workflow triggers
(``InterviewScheduled`` / ``InterviewCompleted`` with ``application_id`` + ``stage_name``) and notifications
(``InterviewScheduled.interviewer_user_ids``, ``ScorecardDue.interviewer_user_id``; ``actor_user_id`` is never
notified about their own action).
"""

import dataclasses
import uuid
from datetime import datetime


@dataclasses.dataclass(frozen=True, slots=True)
class InterviewScheduled:
    tenant_id: uuid.UUID
    interview_id: uuid.UUID
    application_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    stage_name: str
    starts_at: datetime
    interviewer_user_ids: tuple[uuid.UUID, ...] = ()
    actor_user_id: uuid.UUID | None = None
    send_invites: bool = True
    origin_chain_depth: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class InterviewRescheduled:
    tenant_id: uuid.UUID
    interview_id: uuid.UUID
    application_id: uuid.UUID
    sequence: int
    starts_at: datetime
    removed_participant_ids: tuple[uuid.UUID, ...] = ()
    actor_user_id: uuid.UUID | None = None
    send_invites: bool = True


@dataclasses.dataclass(frozen=True, slots=True)
class InterviewCancelled:
    tenant_id: uuid.UUID
    interview_id: uuid.UUID
    application_id: uuid.UUID
    sequence: int
    actor_user_id: uuid.UUID | None = None
    actor_kind: str = "user"
    send_invites: bool = True


@dataclasses.dataclass(frozen=True, slots=True)
class InterviewCompleted:
    tenant_id: uuid.UUID
    interview_id: uuid.UUID
    application_id: uuid.UUID
    demand_id: uuid.UUID
    stage_name: str
    status: str = "completed"  # completed | no_show
    actor_kind: str = "system"


@dataclasses.dataclass(frozen=True, slots=True)
class ScorecardSubmitted:
    tenant_id: uuid.UUID
    interview_id: uuid.UUID
    application_id: uuid.UUID
    scorecard_id: uuid.UUID
    participant_id: uuid.UUID
    source: str  # in_app | secure_link
    author_id: uuid.UUID | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ScorecardDue:
    """A required internal scorecard is missing ``scorecard_reminder_after_hours`` after the interview."""

    tenant_id: uuid.UUID
    interview_id: uuid.UUID
    application_id: uuid.UUID
    interviewer_user_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class InterviewStageOutcomeRecorded:
    tenant_id: uuid.UUID
    application_id: uuid.UUID
    stage_id: uuid.UUID
    outcome: str  # advance | reject | hold
    reason_code: str | None = None
    decided_by: uuid.UUID | None = None
    can_select: bool = False
