# ruff: noqa: E501
"""Assessment DTOs (Epic 40; Epic 36 contract, AD-39).

``AssessmentInviteIn`` / ``AssessmentInvitationSummary`` are FROZEN (contract): changing a field needs a spine
amendment. Everything below them is Epic 40's internal API (``/api/v1/assessment*``). Candidate-facing models live in
``app/schemas/assessments_delivery.py`` and never carry keys, rubrics or item ids.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.money import DecimalStr

__all__ = ["AssessmentInvitationSummary", "AssessmentInviteIn", "ContextKind"]

ContextKind = Literal["application", "campus_drive", "event", "direct"]


class AssessmentInviteIn(BaseModel):
    """``assessments_service.invite`` input; ``gating=None`` takes the assessment default."""

    model_config = ConfigDict(frozen=True)

    assessment_id: uuid.UUID
    candidate_id: uuid.UUID
    context_kind: ContextKind
    context_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None
    gating: bool | None = None
    deadline_at: datetime | None = None
    accommodation_factor: DecimalStr = Field(default=Decimal("1.00"), ge=Decimal("1.00"), le=Decimal("3.00"))
    send_notice: bool = True
    idempotency_key: str | None = None


class AssessmentInvitationSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    assessment_id: uuid.UUID
    candidate_id: uuid.UUID
    application_id: uuid.UUID | None
    context_kind: str
    context_id: uuid.UUID | None
    status: str
    gating: bool
    deadline_at: datetime | None
    result: str | None
    score: DecimalStr | None
    scored_at: datetime | None


# ====================================================================== Epic 40 internal API
ItemKind = Literal[
    "single", "multi", "true_false", "numeric", "short_text", "code_output", "code_fill", "code_order",
    "code_find_bug", "code_freeform", "essay",
]  # fmt: skip
Points = Annotated[Decimal, Field(gt=0, le=Decimal("1000"), max_digits=6, decimal_places=2)]
Pct = Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]
ShortText = Annotated[str, Field(min_length=1, max_length=200)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ItemBody(_In):
    """Body of an item edit (a new version). Per-kind ``options`` / ``answer_key`` / ``rubric`` are validated by
    ``services/assessments/items.py`` (pydantic, ``extra='forbid'``: no harness / stdin / tests keys)."""

    skill_keys: Annotated[list[str], Field(max_length=20)] = Field(default_factory=list)
    tags: Annotated[list[Annotated[str, Field(min_length=1, max_length=60)]], Field(max_length=20)] = Field(
        default_factory=list
    )
    difficulty: int = Field(default=3, ge=1, le=5)
    stem: str = Field(min_length=1, max_length=20_000)
    language: str | None = Field(default=None, max_length=40)
    code: str | None = Field(default=None, max_length=20_000)
    options: dict[str, Any] = Field(default_factory=dict)
    points: Points = Decimal("1")
    answer_key: dict[str, Any] | None = None
    rubric: dict[str, Any] | None = None


class ItemCreate(ItemBody):
    kind: ItemKind


class ItemVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: int
    stem: str
    language: str | None
    code: str | None
    options: dict[str, Any]
    points: DecimalStr
    answer_key: dict[str, Any] | None
    rubric: dict[str, Any] | None
    created_at: datetime


class ItemOut(BaseModel):
    id: uuid.UUID
    human_id: str
    kind: str
    skill_keys: list[str]
    tags: list[str]
    difficulty: int
    current_version: int
    archived_at: datetime | None
    usage_count: int = 0
    created_at: datetime
    updated_at: datetime
    version: ItemVersionOut


class ImportRowOut(BaseModel):
    row: int
    kind: str | None
    stem: str | None
    errors: list[str]
    valid: bool


class ImportPreviewOut(BaseModel):
    batch_id: uuid.UUID
    rows: list[ImportRowOut]
    valid_count: int
    error_count: int


class ImportCommitOut(BaseModel):
    created: int
    item_ids: list[uuid.UUID]


class AssessmentItemImportIn(BaseModel):
    """Go-live import row (FR-263; ``assessments_service.import_assessment_item``). Field names = CSV columns."""

    kind: ItemKind
    stem: str = Field(min_length=1, max_length=20_000)
    points: Points = Decimal("1")
    difficulty: int = Field(default=3, ge=1, le=5)
    tags: str | None = None  # ";"-separated
    skill_keys: str | None = None  # ";"-separated SkillKey strings
    language: str | None = None
    code: str | None = None
    options: str | None = (
        None  # ";"-separated choice texts (single / multi / true_false) or lines (code_order)
    )
    answer: str | None = None  # per kind, see ``item_import.py``
    rubric: str | None = None  # ";"-separated "label:max_points"


# ---------------------------------------------------------------- assessments / sections
class DrawRule(_In):
    skill_keys: Annotated[list[str], Field(max_length=20)] = Field(default_factory=list)
    tags: Annotated[list[str], Field(max_length=20)] = Field(default_factory=list)
    difficulty: tuple[int, int] = (1, 5)
    count: int = Field(ge=1, le=200)


class FixedItemRef(_In):
    item_id: uuid.UUID
    points: Points | None = None


class DraftSection(_In):
    key: Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")]
    title: str = Field(min_length=1, max_length=200)
    time_limit_minutes: int | None = Field(default=None, ge=1, le=600)
    items: Annotated[list[FixedItemRef], Field(max_length=200)] | None = None
    draw: DrawRule | None = None


class DraftIn(_In):
    sections: Annotated[list[DraftSection], Field(max_length=20)]


class AssessmentFields(_In):
    description: str | None = Field(default=None, max_length=5000)
    instructions: str | None = Field(default=None, max_length=20_000)
    time_limit_minutes: int | None = Field(default=None, ge=1, le=600)
    pass_mark_pct: Pct | None = None
    shuffle_items: bool = False
    shuffle_options: bool = True
    attempts_limit: int = Field(default=1, ge=1, le=10)
    gating_default: bool = True
    auto_pass_threshold_pct: Pct | None = None
    skill_keys: Annotated[list[str], Field(max_length=30)] = Field(default_factory=list)
    provider_config_id: uuid.UUID | None = None
    provider_test_ref: str | None = Field(default=None, max_length=200)


class AssessmentCreate(AssessmentFields):
    name: str = Field(min_length=1, max_length=200)
    delivery: Literal["builtin", "provider"] = "builtin"


class AssessmentPatch(_In):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    delivery: Literal["builtin", "provider"] | None = None
    description: str | None = Field(default=None, max_length=5000)
    instructions: str | None = Field(default=None, max_length=20_000)
    time_limit_minutes: int | None = Field(default=None, ge=1, le=600)
    pass_mark_pct: Pct | None = None
    shuffle_items: bool | None = None
    shuffle_options: bool | None = None
    attempts_limit: int | None = Field(default=None, ge=1, le=10)
    gating_default: bool | None = None
    auto_pass_threshold_pct: Pct | None = None
    skill_keys: Annotated[list[str], Field(max_length=30)] | None = None
    provider_config_id: uuid.UUID | None = None
    provider_test_ref: str | None = Field(default=None, max_length=200)


class VersionSectionOut(BaseModel):
    key: str
    title: str
    time_limit_minutes: int | None
    kind: Literal["fixed", "draw"]
    item_count: int
    points: DecimalStr
    pool_size: int | None


class VersionOut(BaseModel):
    id: uuid.UUID
    version: int
    published_at: datetime
    published_by: uuid.UUID | None
    total_points: DecimalStr
    sections: list[VersionSectionOut]


class VersionBrief(BaseModel):
    id: uuid.UUID
    version: int
    published_at: datetime
    total_points: DecimalStr


class AssessmentOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    status: str
    delivery: str
    owner_id: uuid.UUID
    time_limit_minutes: int | None
    pass_mark_pct: DecimalStr | None
    gating_default: bool
    current_version_id: uuid.UUID | None
    current_version_no: int | None
    created_at: datetime
    updated_at: datetime


class AssessmentDetailOut(AssessmentOut):
    instructions: str | None
    shuffle_items: bool
    shuffle_options: bool
    attempts_limit: int
    auto_pass_threshold_pct: DecimalStr | None
    skill_keys: list[str]
    provider_config_id: uuid.UUID | None
    provider_test_ref: str | None
    draft_sections: list[dict[str, Any]]
    current_version: VersionOut | None
    versions: list[VersionBrief]


class PublishOut(BaseModel):
    version: VersionOut
    warnings: list[str]


class ProviderTestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    config_id: uuid.UUID
    external_ref: str
    name: str
    duration_minutes: int | None
    skills: list[Any]
    synced_at: datetime


class CatalogueSyncIn(_In):
    config_id: uuid.UUID


class SkillMapRow(_In):
    provider_skill: ShortText
    skill_key: Annotated[str, Field(min_length=1, max_length=80)]


class SkillMapIn(_In):
    config_id: uuid.UUID
    rows: Annotated[list[SkillMapRow], Field(max_length=500)]


class SkillMapOut(BaseModel):
    config_id: uuid.UUID
    rows: list[dict[str, str]]


class SettingsOut(BaseModel):
    allow_auto_pass_non_gating: bool
    short_answer_confidence_threshold: DecimalStr
    reminder_hours_before: int
    default_deadline_days: int
    link_grace_days: int
    max_signals_per_minute: int
    max_signals_per_attempt: int
    multi_choice_partial_credit: bool


class SettingsIn(_In):
    allow_auto_pass_non_gating: bool | None = None
    short_answer_confidence_threshold: Annotated[Decimal, Field(ge=0, le=1)] | None = None
    reminder_hours_before: int | None = Field(default=None, ge=1, le=168)
    default_deadline_days: int | None = Field(default=None, ge=1, le=60)
    link_grace_days: int | None = Field(default=None, ge=0, le=14)
    max_signals_per_minute: int | None = Field(default=None, ge=1, le=600)
    max_signals_per_attempt: int | None = Field(default=None, ge=10, le=20_000)
    multi_choice_partial_credit: bool | None = None


# ---------------------------------------------------------------- invitations
class BulkInviteIn(_In):
    candidate_ids: Annotated[list[uuid.UUID], Field(max_length=500)] | None = None
    application_ids: Annotated[list[uuid.UUID], Field(max_length=500)] | None = None
    pool_id: uuid.UUID | None = None
    deadline_at: datetime | None = None
    gating: bool | None = None
    accommodation_factor: Annotated[Decimal, Field(ge=Decimal("1.00"), le=Decimal("3.00"))] = Decimal("1.00")
    screen_reader_mode: bool = False


class BulkResult(BaseModel):
    candidate_id: uuid.UUID | None
    application_id: uuid.UUID | None
    status: Literal["created", "exists", "error"]
    invitation_id: uuid.UUID | None
    error_code: str | None


class BulkInviteOut(BaseModel):
    results: list[BulkResult]


class InvitationPatch(_In):
    screen_reader_mode: bool | None = None
    accommodation_factor: Annotated[Decimal, Field(ge=Decimal("1.00"), le=Decimal("3.00"))] | None = None


class ExtendIn(_In):
    deadline_at: datetime


class CancelIn(_In):
    reason: str = Field(min_length=1, max_length=1000)


class DecideIn(_In):
    result: Literal["pass", "fail"]
    note: str | None = Field(default=None, max_length=2000)


class ManualSkillScore(_In):
    skill_key: Annotated[str, Field(min_length=1, max_length=80)]
    proficiency: int = Field(ge=1, le=5)


class ManualResultIn(_In):
    score_pct: Pct
    result_note: str | None = Field(default=None, max_length=2000)
    report_file_id: uuid.UUID | None = None
    skill_scores: Annotated[list[ManualSkillScore], Field(max_length=50)] = Field(default_factory=list)


class InvitationOut(BaseModel):
    id: uuid.UUID
    assessment_id: uuid.UUID
    assessment_name: str
    delivery: str
    candidate_id: uuid.UUID | None
    candidate_name: str | None
    application_id: uuid.UUID | None
    context_kind: str
    context_id: uuid.UUID | None
    status: str
    gating: bool
    result: str | None
    score: DecimalStr | None
    scored_at: datetime | None
    deadline_at: datetime
    percentile: DecimalStr | None
    points: DecimalStr | None
    max_points: DecimalStr | None
    section_scores: dict[str, Any]
    skill_scores: dict[str, Any]
    accommodation_factor: DecimalStr
    screen_reader_mode: bool
    extended_count: int
    report_url: str | None
    report_file_id: uuid.UUID | None
    provider_ref: str | None
    decided_at: datetime | None
    decision_note: str | None
    cancel_reason: str | None
    created_at: datetime
    attempt_id: uuid.UUID | None
    signal_counts: dict[str, int]
    pending_reviews: int
    score_hidden_reason: str | None = None  # Epic 49: AEDT score suppressor


class BadgeOut(BaseModel):
    application_id: uuid.UUID
    invitation_id: uuid.UUID
    status: str
    score: DecimalStr | None
    result: str | None
    score_hidden_reason: str | None = None  # Epic 49: AEDT score suppressor


class BadgesOut(BaseModel):
    items: list[BadgeOut]


# ---------------------------------------------------------------- reviews / proctoring
class ReviewEntry(BaseModel):
    reviewer_id: uuid.UUID | None
    score: DecimalStr
    note: str | None
    created_at: datetime


class ReviewItemOut(BaseModel):
    answer_id: uuid.UUID
    attempt_id: uuid.UUID
    invitation_id: uuid.UUID
    assessment_name: str
    candidate_name: str | None
    position: int
    section_key: str
    kind: str
    stem: str
    code: str | None
    language: str | None
    response: dict[str, Any] | None
    max_points: DecimalStr
    auto_score: DecimalStr | None
    auto_confidence: DecimalStr | None
    needs_review: bool
    final_score: DecimalStr | None
    scored_via: str | None
    rubric: dict[str, Any] | None
    reviews: list[ReviewEntry]


class ReviewIn(_In):
    score: Annotated[Decimal, Field(ge=0, max_digits=6, decimal_places=2)]
    rubric_marks: dict[str, Annotated[Decimal, Field(ge=0)]] | None = None
    note: str | None = Field(default=None, max_length=2000)


class AiSuggestionOut(BaseModel):
    suggested_score: DecimalStr
    rationale: str
    advisory: Literal[True] = True
    ai_action_id: uuid.UUID | None


class AiSuggestionModel(BaseModel):
    """Model output schema of ``assessment_review_suggest`` (advisory only)."""

    suggested_score: Decimal = Field(ge=0)
    rationale: str = Field(max_length=400)


class SignalOut(BaseModel):
    id: uuid.UUID
    kind: str
    occurred_at: datetime
    client_ts: datetime | None
    detail: dict[str, Any] | None


class AnnotationIn(_In):
    signal_id: uuid.UUID | None = None
    note: str = Field(min_length=1, max_length=2000)


class AnnotationOut(BaseModel):
    id: uuid.UUID
    signal_id: uuid.UUID | None
    note: str
    created_by: uuid.UUID | None
    created_at: datetime


class SignalsOut(BaseModel):
    counts: dict[str, int]
    dropped: int
    timeline: list[SignalOut]
    annotations: list[AnnotationOut]


class SharesIn(_In):
    invitation_ids: Annotated[list[uuid.UUID], Field(max_length=20)]


class SharesOut(BaseModel):
    invitation_ids: list[uuid.UUID]
