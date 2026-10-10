"""Interview API models (Epic 7: plans, kits, templates, interviews, scorecards, outcomes, public scorecards).

Times: requests carry ``start_local`` (naive ``YYYY-MM-DDTHH:MM``) + an IANA ``timezone``; the server converts
with ``zoneinfo`` and responses carry UTC instants (AD-14) plus display strings.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

StageKind = Literal["technical", "hr", "client", "managerial", "other", "assessment"]  # Epic 40: +assessment
InterviewMode = Literal["video", "phone", "in_person"]
InterviewStatus = Literal["scheduled", "completed", "cancelled", "no_show"]
ParticipantKind = Literal["interviewer_internal", "interviewer_external", "candidate", "observer"]
Recommendation = Literal["strong_yes", "yes", "no", "strong_no"]
ScorecardStatus = Literal["draft", "submitted"]
Outcome = Literal["advance", "reject", "hold"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Key = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z0-9][a-z0-9_\-]{0,49}$")]
Duration = Annotated[int, Field(ge=15, le=480)]
Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_lower=True, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320
    ),
]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ settings
class InterviewSettingsOut(BaseModel):
    reminder_offsets_minutes: list[int]
    scorecard_reminder_after_hours: int
    external_scorecard_link_days_after: int
    default_duration_minutes: int
    rating_scale: int


class InterviewSettingsUpdate(_In):
    reminder_offsets_minutes: (
        Annotated[list[Annotated[int, Field(ge=5, le=10080)]], Field(max_length=5)] | None
    ) = None
    scorecard_reminder_after_hours: Annotated[int, Field(ge=1, le=720)] | None = None
    external_scorecard_link_days_after: Annotated[int, Field(ge=1, le=90)] | None = None
    default_duration_minutes: Duration | None = None
    rating_scale: Annotated[int, Field(ge=3, le=10)] | None = None


# ================================================================ kits
class Competency(_In):
    key: Key
    label: Name
    guidance: Annotated[str, StringConstraints(max_length=4000)] | None = None


class KitQuestion(_In):
    key: Key
    competency_key: Key | None = None
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    guidance: Annotated[str, StringConstraints(max_length=4000)] | None = None


class KitCreate(_In):
    name: Name
    description: Annotated[str, StringConstraints(max_length=4000)] | None = None
    competencies: Annotated[list[Competency], Field(min_length=1, max_length=30)]
    questions: Annotated[list[KitQuestion], Field(max_length=100)] = []
    rating_scale: Annotated[int, Field(ge=3, le=10)] = 5


class KitUpdate(_In):
    name: Name | None = None
    description: Annotated[str, StringConstraints(max_length=4000)] | None = None
    competencies: Annotated[list[Competency], Field(min_length=1, max_length=30)] | None = None
    questions: Annotated[list[KitQuestion], Field(max_length=100)] | None = None
    rating_scale: Annotated[int, Field(ge=3, le=10)] | None = None


class KitOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    competencies: list[Competency]
    questions: list[KitQuestion]
    rating_scale: int
    recommendation_options: list[Recommendation]
    version: int
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ================================================================ templates / plans
class TemplateStage(_In):
    name: Name
    kind: StageKind = "other"
    kit_id: uuid.UUID | None = None
    default_duration_minutes: Duration = 60


class TemplateCreate(_In):
    name: Name
    stages: Annotated[list[TemplateStage], Field(min_length=1, max_length=20)]
    is_default: bool = False


class TemplateUpdate(_In):
    name: Name | None = None
    stages: Annotated[list[TemplateStage], Field(min_length=1, max_length=20)] | None = None
    is_default: bool | None = None
    archived: bool | None = None


class TemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    stages: list[TemplateStage]
    is_default: bool
    archived_at: datetime | None
    created_at: datetime


class PlanStageIn(_In):
    id: uuid.UUID | None = None
    name: Name
    kind: StageKind = "other"
    kit_id: uuid.UUID | None = None
    default_duration_minutes: Duration = 60
    assessment_id: uuid.UUID | None = None  # Epic 40: required (published) when kind == "assessment"


class PlanSave(_In):
    stages: Annotated[list[PlanStageIn], Field(max_length=20)]


class ApplyTemplateIn(_In):
    template_id: uuid.UUID


class PlanStageOut(BaseModel):
    id: uuid.UUID
    position: int
    name: str
    kind: StageKind
    kit_id: uuid.UUID | None
    kit_name: str | None
    default_duration_minutes: int
    is_final: bool
    interview_counts: dict[str, int]
    assessment_id: uuid.UUID | None = None  # Epic 40


class PlanOut(BaseModel):
    demand_id: uuid.UUID
    stages: list[PlanStageOut]
    has_interviews: bool


# ================================================================ interviews
class ParticipantIn(_In):
    kind: Literal["interviewer_internal", "interviewer_external", "observer"]
    user_id: uuid.UUID | None = None
    email: Email | None = None
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    timezone: str | None = None
    scorecard_required: bool | None = None


class InterviewCreate(_In):
    stage_id: uuid.UUID
    start_local: Annotated[str, StringConstraints(strip_whitespace=True, max_length=25)]
    timezone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)]
    duration_minutes: int | None = None
    mode: InterviewMode = "video"
    meeting_url: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None
    location: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    notes: Annotated[str, StringConstraints(max_length=8000)] | None = None
    create_online_meeting: bool = False
    send_invites: bool = True
    allow_past: bool = False
    candidate_timezone: str | None = None
    participants: Annotated[list[ParticipantIn], Field(max_length=30)]

    @field_validator("meeting_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value and not value.lower().startswith(("https://", "http://")):
            msg = "meeting_url must be an http(s) URL"
            raise ValueError(msg)
        return value or None


class InterviewReschedule(_In):
    start_local: Annotated[str, StringConstraints(strip_whitespace=True, max_length=25)]
    timezone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)]
    duration_minutes: int | None = None
    expected_version: int
    participants: Annotated[list[ParticipantIn], Field(max_length=30)] | None = None
    mode: InterviewMode | None = None
    meeting_url: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None
    location: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    notes: Annotated[str, StringConstraints(max_length=8000)] | None = None
    candidate_timezone: str | None = None


class CancelIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class ParticipantOut(BaseModel):
    id: uuid.UUID
    kind: ParticipantKind
    user_id: uuid.UUID | None
    email: str
    name: str | None
    timezone: str
    local_time: str
    response_status: str
    scorecard_required: bool
    scorecard_status: ScorecardStatus | None


class InterviewOut(BaseModel):
    id: uuid.UUID
    human_id: str
    application_id: uuid.UUID
    demand_id: uuid.UUID
    demand_title: str | None
    candidate_id: uuid.UUID
    candidate_name: str | None
    stage_id: uuid.UUID
    stage_name: str
    round: int
    starts_at: datetime
    ends_at: datetime
    duration_minutes: int
    timezone: str
    start_local: str
    display_time: str
    mode: InterviewMode
    meeting_url: str | None
    location: str | None
    notes: str | None
    status: InterviewStatus
    sequence: int
    organizer_user_id: uuid.UUID
    provider: str
    create_online_meeting: bool
    cancelled_reason: str | None
    version: int
    participants: list[ParticipantOut]
    scorecards_submitted: int
    scorecards_required: int
    created_at: datetime


# ================================================================ scorecards
class RatingIn(_In):
    competency_key: Key
    rating: int
    note: Annotated[str, StringConstraints(max_length=4000)] | None = None


class AnswerIn(_In):
    question_key: Key
    note: Annotated[str, StringConstraints(max_length=8000)] | None = None


class ScorecardDraftIn(_In):
    ratings: Annotated[list[RatingIn], Field(max_length=30)] = []
    answers: Annotated[list[AnswerIn], Field(max_length=100)] = []
    recommendation: Recommendation | None = None
    summary: Annotated[str, StringConstraints(max_length=8000)] | None = None


class ScorecardOut(BaseModel):
    id: uuid.UUID | None
    interview_id: uuid.UUID
    participant_id: uuid.UUID
    participant_name: str | None
    participant_kind: ParticipantKind
    author_user_id: uuid.UUID | None
    kit: dict[str, object] | None
    ratings: list[RatingIn]
    answers: list[AnswerIn]
    recommendation: Recommendation | None
    summary: str | None
    status: ScorecardStatus
    submitted_at: datetime | None
    source: str


class ScorecardListOut(BaseModel):
    hidden: bool
    submitted_count: int
    required_count: int
    items: list[ScorecardOut]


# ================================================================ outcomes / summary
class StageOutcomeIn(_In):
    stage_id: uuid.UUID
    outcome: Outcome
    reason_code: Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)] | None = None
    comment: Annotated[str, StringConstraints(max_length=4000)] | None = None


class StageOutcomeOut(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    stage_id: uuid.UUID
    outcome: Outcome
    reason_code: str | None
    comment: str | None
    decided_by: uuid.UUID | None
    decided_at: datetime
    superseded_at: datetime | None
    can_select: bool


class RecommendationCounts(BaseModel):
    strong_yes: int = 0
    yes: int = 0
    no: int = 0
    strong_no: int = 0


class StageSummaryOut(BaseModel):
    stage: PlanStageOut
    interviews: list[InterviewOut]
    scorecards_submitted: int
    scorecards_required: int
    hidden: bool
    recommendations: RecommendationCounts | None
    latest_outcome: StageOutcomeOut | None


class InterviewSummaryOut(BaseModel):
    application_id: uuid.UUID
    stages: list[StageSummaryOut]
    can_select: bool


class SelectOut(BaseModel):
    application_id: uuid.UUID
    stage_key: str


class BoardBadge(BaseModel):
    next_interview_at: datetime | None
    scorecards_pending: int


# ================================================================ public scorecard link (NFR-17)
class PublicCompetency(BaseModel):
    key: str
    label: str
    guidance: str | None = None


class PublicKitQuestion(BaseModel):
    key: str
    competency_key: str | None = None
    text: str
    guidance: str | None = None


class PublicScorecardView(BaseModel):
    state: Literal["not_started", "open", "submitted"]
    candidate_display_name: str
    demand_title: str | None
    stage_name: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    display_time: str
    rating_scale: int
    recommendation_options: list[Recommendation]
    competencies: list[PublicCompetency]
    questions: list[PublicKitQuestion]


class PublicScorecardIn(_In):
    ratings: Annotated[list[RatingIn], Field(max_length=30)]
    answers: Annotated[list[AnswerIn], Field(max_length=100)] = []
    recommendation: Recommendation
    summary: Annotated[str, StringConstraints(max_length=8000)] | None = None
