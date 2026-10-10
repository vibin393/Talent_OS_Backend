"""Pipeline domain events (Epic 6; dispatched after commit, consumed by Epic 10). Ids and enums only, no PII.

``origin_chain_depth`` lets the workflow engine (Epic 10) detect rule loops; it is copied from
``ctx.workflow_chain_depth`` when the change happened under a workflow context (default 0).
Consent events are Epic 12's (``app.events.privacy.ConsentGranted`` / ``ConsentWithdrawn``).
"""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class ApplicationCreated:
    tenant_id: uuid.UUID
    application_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    source: str
    actor_kind: str
    origin_chain_depth: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class ApplicationStageChanged:
    tenant_id: uuid.UUID
    application_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    from_key: str | None
    to_key: str
    actor_kind: str
    reason_code: str | None = None
    origin_chain_depth: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class RejectionEmailRequested:
    """Internal: enqueue the tenant rejection email for a just-rejected application."""

    tenant_id: uuid.UUID
    application_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class ScreeningCompleted:
    tenant_id: uuid.UUID
    screening_id: uuid.UUID
    application_id: uuid.UUID
    kind: str
    outcome: str


@dataclasses.dataclass(frozen=True, slots=True)
class AIScreeningSent:
    tenant_id: uuid.UUID
    screening_id: uuid.UUID
    application_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class AIScreeningAnswered:
    tenant_id: uuid.UUID
    screening_id: uuid.UUID
    application_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class SubmissionCreated:
    tenant_id: uuid.UUID
    submission_id: uuid.UUID
    application_id: uuid.UUID
    demand_id: uuid.UUID
    client_id: uuid.UUID
    status: str
    below_min_margin: bool
    origin_chain_depth: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class SubmissionSent:
    tenant_id: uuid.UUID
    submission_id: uuid.UUID
    application_id: uuid.UUID
    demand_id: uuid.UUID
    client_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class ClientFeedbackReceived:
    tenant_id: uuid.UUID
    feedback_id: uuid.UUID
    submission_id: uuid.UUID
    application_id: uuid.UUID
    decision: str
    source: str


@dataclasses.dataclass(frozen=True, slots=True)
class AIScreeningSummaryRequested:
    """Internal: (re)run the AI summary of an answered screening (retry endpoint)."""

    tenant_id: uuid.UUID
    screening_id: uuid.UUID
