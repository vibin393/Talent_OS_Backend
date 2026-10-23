# ruff: file-ignore[line-too-long]
"""Public API v1 inbound data-loop contracts (unified platform hub ingest).

* ``POST /public/v1/assessments`` (Croar -> ATS): ``AssessmentIngestIn`` -> ``AssessmentOut``.
* ``POST /public/v1/engagement`` (Feedback IQ -> ATS): ``EngagementIngestIn`` -> ``EngagementOut``.

Inputs forbid unknown fields (``PublicIn``); outputs list explicit fields (``PublicObject``)."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.public_v1.common import PublicIn, PublicObject


# ================================================================ assessments (Croar -> ATS)
class CandidateRef(PublicIn):
    id: uuid.UUID | None = None
    external_id: str | None = Field(default=None, max_length=128, description="The candidate's human id (e.g. CAND-0001)")
    email: str | None = Field(default=None, max_length=320, description="The candidate's email — used to resolve the candidate when the caller (e.g. Croar) does not know the ATS id/human_id")


class ApplicationRef(PublicIn):
    id: uuid.UUID | None = None


class AssessmentPayload(PublicIn):
    type: str = Field(max_length=60)
    provider: str = Field(max_length=120)
    name: str = Field(max_length=200)
    score: float | None = None
    max_score: float | None = None
    status: str = Field(max_length=40)
    completed_at: datetime | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class AssessmentIngestIn(PublicIn):
    event_id: str = Field(max_length=128, description="Idempotency key of the source event (unique per tenant)")
    candidate_ref: CandidateRef
    application_ref: ApplicationRef | None = None
    assessment: AssessmentPayload


class AssessmentOut(PublicObject):
    event_id: str
    candidate_id: uuid.UUID
    application_id: uuid.UUID | None = None
    type: str
    provider: str
    name: str
    score: float | None = None
    max_score: float | None = None
    status: str
    completed_at: datetime | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


# ================================================================ engagement (Feedback IQ -> ATS)
class SubjectRef(PublicIn):
    email: str | None = Field(default=None, max_length=320)
    ats_candidate_id: str | None = Field(default=None, max_length=128)


class EngagementPayload(PublicIn):
    type: str = Field(max_length=60)
    source: str = Field(max_length=120)
    score: float | None = None
    summary: str = Field(max_length=2000)
    occurred_at: datetime | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class EngagementIngestIn(PublicIn):
    event_id: str = Field(max_length=128, description="Idempotency key of the source event (unique per tenant)")
    subject_ref: SubjectRef
    signal: EngagementPayload


class EngagementOut(PublicObject):
    event_id: str
    subject_email: str | None = None
    subject_ats_candidate_id: str | None = None
    matched_candidate_id: uuid.UUID | None = None
    matched: bool = False
    type: str
    source: str
    score: float | None = None
    summary: str
    occurred_at: datetime | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
