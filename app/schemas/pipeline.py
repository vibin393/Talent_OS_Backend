"""Pipeline API models (Epic 6). Money is ``{"amount": "1800.00", "currency": "INR"}`` (AD-12).

Compensation fields (``pay_rate``, ``bill_rate``, ``margin_*``) are masked by the service with
``core.permissions.mask_compensation`` for principals without ``compensation:view``.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.schemas.interviews import PublicScorecardView
from app.schemas.offers import CandidateOfferView
from app.schemas.placements_portal import JoiningLinkInfo  # Epic 19
from app.schemas.privacy import NoticeOut, PublicLinkOut

StageKind = Literal["anchored", "custom"]
ReasonKind = Literal["reject", "withdraw", "reopen"]
ApplicationSource = Literal[
    "manual", "search", "match", "ai_match", "saved_list", "candidate_360", "career_portal", "import", "api",
    "public_api",  # Epic 67 (Epic 66 500_01 widened the CHECK)
]
QuestionType = Literal["text", "number", "boolean", "choice", "rating", "money", "date"]
MappedField = Literal[
    "expected_ctc", "notice_period_days", "current_location", "work_authorization", "willing_to_relocate"
]
ScreeningOutcome = Literal["proceed", "hold", "reject"]
ScreeningStatus = Literal["draft", "sent", "consent_declined", "answered", "reviewed", "completed", "expired"]
SubmissionStatus = Literal["draft", "pending_approval", "approved", "sent", "cancelled", "approval_rejected"]
FeedbackStatus = Literal["none", "interested", "rejected_pending", "rejected", "on_hold"]
FeedbackDecision = Literal["interested", "rejected", "on_hold"]
ResumeKind = Literal["original", "client_formatted"]
RateUnit = Literal["hour", "day", "month", "year"]
ConsentMode = Literal["candidate_link", "attestation_allowed"]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Code = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_]{1,49}$")]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Money(BaseModel):
    amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    currency: Currency


class MoneyOut(BaseModel):
    amount: str
    currency: str


# ================================================================ settings
class PipelineSettingsOut(BaseModel):
    submission_consent_mode: ConsentMode
    prior_submission_window_days: int
    min_margin_pct: Decimal
    burden_pct: dict[str, Decimal]
    rejection_email_enabled: bool
    rejection_email_subject: str
    rejection_email_body: str
    auto_interview_on_interest: bool
    show_bill_rate_in_submission: bool
    link_days: dict[str, int] = Field(
        description="Secure-link expiries (read-only here; tenant settings hub)"
    )


class PipelineSettingsUpdate(_In):
    submission_consent_mode: ConsentMode | None = None
    prior_submission_window_days: int | None = Field(default=None, ge=0, le=3650)
    min_margin_pct: Decimal | None = Field(default=None, ge=-100, le=100, decimal_places=2)
    burden_pct: dict[str, Decimal] | None = None
    rejection_email_enabled: bool | None = None
    rejection_email_subject: str | None = Field(default=None, max_length=300)
    rejection_email_body: str | None = Field(default=None, max_length=20000)
    auto_interview_on_interest: bool | None = None
    show_bill_rate_in_submission: bool | None = None

    @field_validator("burden_pct")
    @classmethod
    def _burden(cls, value: dict[str, Decimal] | None) -> dict[str, Decimal] | None:
        if value is None:
            return None
        allowed = {"contract", "contract_to_hire", "permanent", "freelance"}
        for key, pct in value.items():
            if key not in allowed:
                msg = f"unknown employment type {key!r}"
                raise ValueError(msg)
            if pct < 0 or pct > 200:
                msg = "burden % must be between 0 and 200"
                raise ValueError(msg)
        return value


# ================================================================ stages
class StageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    label: str
    kind: StageKind
    position: int
    archived_at: datetime | None
    terminal: bool = False


class StageTransitionOut(BaseModel):
    from_stage_id: uuid.UUID | None
    to_stage_id: uuid.UUID
    actors: list[str]
    reason_required: bool = False


class StagesOut(BaseModel):
    items: list[StageOut]
    transitions: list[StageTransitionOut] | None = None


class StageCreate(_In):
    label: Label
    key: Code | None = None


class StageUpdate(_In):
    label: Label


class StageReorder(_In):
    stage_ids: list[uuid.UUID] = Field(min_length=1, max_length=50, description="Custom stages, in order")


class StageArchive(_In):
    move_to_stage_id: uuid.UUID | None = None


class ReasonCodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: ReasonKind
    code: str
    label: str
    archived_at: datetime | None


class ReasonCodeCreate(_In):
    kind: ReasonKind
    code: Code
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ReasonCodeUpdate(_In):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


# ================================================================ applications
class ApplicationsCreate(_In):
    candidate_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    source: ApplicationSource = "manual"
    source_ref: str | None = Field(default=None, max_length=200)


class ApplicationCreateItem(BaseModel):
    candidate_id: uuid.UUID
    status: Literal["created", "exists", "error"]
    application_id: uuid.UUID | None = None
    existing_application_id: uuid.UUID | None = None
    error: dict[str, Any] | None = None


class ApplicationsCreateOut(BaseModel):
    items: list[ApplicationCreateItem]
    counts: dict[str, int]


class CandidateSummary(BaseModel):
    id: uuid.UUID
    human_id: str | None = None
    full_name: str | None = None
    current_title: str | None = None
    current_employer: str | None = None
    city: str | None = None


class StageRef(BaseModel):
    id: uuid.UUID
    key: str
    label: str
    kind: StageKind


class SubmissionBrief(BaseModel):
    id: uuid.UUID
    human_id: str
    status: SubmissionStatus
    feedback_status: FeedbackStatus
    sent_at: datetime | None


class ApplicationOut(BaseModel):
    id: uuid.UUID
    human_id: str
    demand_id: uuid.UUID
    demand_title: str | None = None
    candidate: CandidateSummary
    stage: StageRef
    stage_entered_at: datetime
    days_in_stage: int
    source: str
    source_ref: str | None
    owner_id: uuid.UUID
    screening_passed: bool
    screening_status: str | None = None
    last_client_feedback: FeedbackDecision | None
    pending_client_rejection_id: uuid.UUID | None = None
    latest_submission: SubmissionBrief | None = None
    match_score: Decimal | None = None
    # Epic 49: set (and ``match_score`` None) when an AEDT score suppressor hides the pair.
    score_hidden_reason: str | None = None
    # Epic 7 board badges (``interviews_service.board_badges``).
    next_interview_at: datetime | None = None
    scorecards_pending: int = 0
    version: int
    created_at: datetime


class StageHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_stage_id: uuid.UUID | None
    to_stage_id: uuid.UUID
    from_key: str | None = None
    to_key: str | None = None
    actor_kind: str
    actor_id: uuid.UUID | None
    reason_code: str | None
    note: str | None
    at: datetime


class ApplicationDetailOut(ApplicationOut):
    history: list[StageHistoryOut]
    screenings: list["ScreeningOut"] = Field(default_factory=list)
    submissions: list[SubmissionBrief] = Field(default_factory=list)


class TransitionIn(_In):
    to_stage_id: uuid.UUID
    reason_code: str | None = Field(default=None, max_length=50)
    note: str | None = Field(default=None, max_length=4000)
    expected_stage_id: uuid.UUID | None = None
    send_rejection_email: bool = False


class BulkTransitionIn(_In):
    application_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    to_stage_id: uuid.UUID
    reason_code: str | None = Field(default=None, max_length=50)
    note: str | None = Field(default=None, max_length=4000)
    send_rejection_email: bool = False


class BulkItemOut(BaseModel):
    application_id: uuid.UUID
    ok: bool
    error: dict[str, Any] | None = None


class BulkTransitionOut(BaseModel):
    items: list[BulkItemOut]
    counts: dict[str, int]


class BoardColumn(BaseModel):
    stage: StageOut
    count: int
    items: list[ApplicationOut]


class BoardOut(BaseModel):
    columns: list[BoardColumn]
    total: int


# ================================================================ questionnaires / screenings
class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,60}$")]
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    type: QuestionType
    options: list[Annotated[str, StringConstraints(min_length=1, max_length=200)]] | None = None
    required: bool = False
    maps_to: MappedField | None = None

    @field_validator("options")
    @classmethod
    def _options(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) > 50:
            msg = "at most 50 options"
            raise ValueError(msg)
        return value


class QuestionnaireOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    questions: list[Question]
    is_default: bool
    archived_at: datetime | None


class QuestionnaireCreate(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    questions: list[Question] = Field(min_length=1, max_length=60)
    is_default: bool = False


class QuestionnaireUpdate(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    questions: list[Question] | None = Field(default=None, min_length=1, max_length=60)
    is_default: bool | None = None


class ScreeningFormOut(BaseModel):
    questionnaire_id: uuid.UUID | None
    questions: list[Question]


class ProposedUpdate(BaseModel):
    field: MappedField
    current: Any = None
    proposed: Any = None


class ScreeningOut(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    candidate_id: uuid.UUID
    kind: Literal["recruiter", "ai"]
    questionnaire_id: uuid.UUID | None
    questions: list[dict[str, Any]]
    answers: dict[str, Any]
    notes: str | None
    outcome: ScreeningOutcome | None
    reason_code: str | None
    status: ScreeningStatus
    ai_summary: dict[str, Any] | None
    ai_generated: bool
    ai_action_id: uuid.UUID | None
    summary_status: str | None
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    secure_link_id: uuid.UUID | None
    link_expires_at: datetime | None = None
    answered_at: datetime | None
    completed_by: uuid.UUID | None
    completed_at: datetime | None
    created_at: datetime
    proposed_candidate_updates: list[ProposedUpdate] = Field(default_factory=list)


class ScreeningCreate(_In):
    questionnaire_id: uuid.UUID | None = None
    answers: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = Field(default=None, max_length=20000)
    outcome: ScreeningOutcome | None = None
    reason_code: str | None = Field(default=None, max_length=50)
    note: str | None = Field(default=None, max_length=4000)
    move_to_shortlisted: bool = False
    send_rejection_email: bool = False


class ScreeningUpdate(_In):
    questions: list[Question] | None = Field(default=None, min_length=1, max_length=60)
    answers: dict[str, Any] | None = None
    notes: str | None = Field(default=None, max_length=20000)


class OutcomeIn(_In):
    outcome: ScreeningOutcome
    reason_code: str | None = Field(default=None, max_length=50)
    note: str | None = Field(default=None, max_length=4000)
    move_to_shortlisted: bool = False
    send_rejection_email: bool = False


class ApplyUpdatesIn(_In):
    fields: list[MappedField] = Field(min_length=1)


class ApplyUpdatesOut(BaseModel):
    applied: list[MappedField]


class AIScreeningCreate(_In):
    action: Literal["generate"] = "generate"


# AI tool schemas (validated by the gateway) --------------------------------------------------------------
class AIQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,60}$")]
    label: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    type: Literal["text", "choice", "number"]
    options: list[Annotated[str, StringConstraints(min_length=1, max_length=200)]] | None = None
    required: bool = True


class QuestionSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[AIQuestion] = Field(min_length=5, max_length=10)


class StructuredAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_key: str = Field(max_length=60)
    answer: str = Field(max_length=2000)


class Gap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_key: str = Field(max_length=60)
    issue: str = Field(max_length=500)


class ScreeningSummary(BaseModel):
    """AI summary tool schema: no ``reject`` value exists (FR-65); any other value is dropped to None."""

    model_config = ConfigDict(extra="forbid")

    structured_answers: list[StructuredAnswer] = Field(default_factory=list, max_length=20)
    gaps: list[Gap] = Field(default_factory=list, max_length=20)
    summary: str = Field(max_length=3000)
    suggested_outcome: Literal["proceed", "hold"] | None = None

    @field_validator("suggested_outcome", mode="before")
    @classmethod
    def _ignore_other(cls, value: Any) -> Any:
        return value if value in ("proceed", "hold") else None


# ================================================================ submissions
class ConsentAttestation(_In):
    text: str = Field(min_length=10, max_length=2000)
    confirmed: bool


class PerApplicationIn(_In):
    pay_rate: Money
    bill_rate: Money
    pay_rate_unit: RateUnit | None = None
    bill_rate_unit: RateUnit | None = None
    resume_kind: ResumeKind = "original"
    prior_submission_reason: str | None = Field(default=None, max_length=2000)
    consent_attestation: ConsentAttestation | None = None


class SubmissionsCreate(_In):
    application_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)
    per_application: dict[uuid.UUID, PerApplicationIn]
    note: str | None = Field(default=None, max_length=5000)
    recipient_contact_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    cc_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    subject: str | None = Field(default=None, max_length=300)
    body_html: str | None = Field(default=None, max_length=20000)


class SubmissionCreateItem(BaseModel):
    application_id: uuid.UUID
    status: Literal["sent", "pending_approval", "blocked"]
    submission_id: uuid.UUID | None = None
    error: dict[str, Any] | None = None


class SubmissionsCreateOut(BaseModel):
    items: list[SubmissionCreateItem]
    counts: dict[str, int]


class MarginPreviewIn(_In):
    demand_id: uuid.UUID | None = None
    employment_type: str | None = None
    rate_unit: RateUnit | None = None
    pay_rate: Money
    bill_rate: Money
    pay_rate_unit: RateUnit | None = None
    bill_rate_unit: RateUnit | None = None


class MarginOut(BaseModel):
    pay_rate: MoneyOut
    bill_rate: MoneyOut
    rate_unit: RateUnit
    burden_pct: str
    margin_amount: MoneyOut
    margin_pct: str
    min_margin_pct: str
    below_min_margin: bool


class SubmissionPreviewIn(_In):
    application_id: uuid.UUID
    resume_kind: ResumeKind = "original"
    bill_rate: Money | None = None
    note: str | None = Field(default=None, max_length=5000)
    recipient_contact_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    subject: str | None = Field(default=None, max_length=300)
    body_html: str | None = Field(default=None, max_length=20000)


class SubmissionPreviewOut(BaseModel):
    subject: str
    html: str
    resume_kind: ResumeKind
    resume_available: bool
    resume_error: str | None = None
    resume_pdf_base64: str | None = None


class FeedbackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    submission_id: uuid.UUID
    application_id: uuid.UUID
    contact_id: uuid.UUID | None
    source: Literal["link", "recorded"]
    decision: FeedbackDecision
    comment: str | None
    recorded_by: uuid.UUID | None
    received_at: datetime
    confirmed_by: uuid.UUID | None
    confirmed_at: datetime | None
    dismissed_at: datetime | None


class SubmissionOut(BaseModel):
    id: uuid.UUID
    human_id: str
    application_id: uuid.UUID
    demand_id: uuid.UUID
    client_id: uuid.UUID
    candidate_id: uuid.UUID
    status: SubmissionStatus
    pay_rate: MoneyOut | None = None
    bill_rate: MoneyOut | None = None
    rate_unit: RateUnit
    employment_type: str | None
    burden_pct: str
    margin_amount: MoneyOut | None = None
    margin_pct: str | None = None
    below_min_margin: bool | None = None
    approval_request_id: uuid.UUID | None
    resume_kind: ResumeKind
    resume_file_id: uuid.UUID | None
    resume_sha256: str | None
    resume_download_url: str | None = None
    note: str | None
    recipient_contact_ids: list[uuid.UUID]
    cc_user_ids: list[uuid.UUID]
    email_subject: str | None
    prior_submission_reason: str | None
    consent_id: uuid.UUID | None
    created_by: uuid.UUID | None
    created_at: datetime
    sent_at: datetime | None
    sent_by: uuid.UUID | None
    cancelled_at: datetime | None
    feedback_status: FeedbackStatus
    feedback: list[FeedbackOut] = Field(default_factory=list)


class FeedbackIn(_In):
    decision: FeedbackDecision
    comment: str | None = Field(default=None, max_length=4000)
    contact_id: uuid.UUID | None = None


class ClientSubmissionSummary(BaseModel):
    total: int
    sent: int
    interested: int
    rejected: int
    pending_feedback: int
    recent: list[dict[str, Any]]


class DemandProgressCounts(BaseModel):
    applications: int
    submissions: int
    interviewing: int


class ConsentRequestIn(_In):
    purpose: Literal["data_processing", "ai_processing", "client_submission", "marketing"] = (
        "client_submission"
    )
    client_id: uuid.UUID | None = None


class SecureLinkOut(BaseModel):
    id: uuid.UUID
    purpose: str
    subject_type: str
    subject_id: uuid.UUID
    expires_at: datetime
    revoked_at: datetime | None
    consumed_at: datetime | None


# ================================================================ public secure-link pages
class PublicQuestion(BaseModel):
    key: str
    label: str
    type: str
    options: list[str] | None = None
    required: bool = False


class PublicLinkView(PublicLinkOut):
    """``GET /public/links/{token}`` view model for every purpose (no ids / emails)."""

    status: str | None = None
    job_title: str | None = None
    client_name: str | None = None
    candidate_first_name: str | None = None
    consent_required: bool | None = None
    questions: list[PublicQuestion] | None = None
    decision: FeedbackDecision | None = None
    decisions: list[FeedbackDecision] | None = None
    # Epic 8: purpose ``offer_response`` -- the candidate offer view (never bill rate or margin).
    offer: CandidateOfferView | None = None
    # Epic 7: purpose ``interview_scorecard`` -- the external interviewer's scorecard page.
    scorecard: PublicScorecardView | None = None
    # Epic 19: purpose ``joining_confirmation`` -- client, role and start date of a pre-joining placement.
    joining: JoiningLinkInfo | None = None
    # Epic 33: purpose ``channel_opt_out`` -- the channel (``sms`` | ``whatsapp``) the recipient can stop.
    channel: str | None = None


class PublicSubmitIn(_In):
    answers: dict[str, Any]


class PublicFeedbackIn(_In):
    decision: FeedbackDecision
    comment: str | None = Field(default=None, max_length=4000)


class PublicActionOut(BaseModel):
    status: str


class PublicNoticeOut(NoticeOut):
    pass


class CandidateApplicationOut(BaseModel):
    id: uuid.UUID
    human_id: str
    demand_id: uuid.UUID
    demand_title: str | None
    client_id: uuid.UUID | None
    stage: StageRef
    stage_entered_at: datetime
    last_client_feedback: FeedbackDecision | None
    created_at: datetime


class DateAnswer(BaseModel):
    value: date


ApplicationDetailOut.model_rebuild()
