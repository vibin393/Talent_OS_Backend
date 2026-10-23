# ruff: file-ignore[line-too-long, ambiguous-unicode-character-string]
"""Bias audit API models (Epic 49). No model here ever carries an individual's self-ID answers except
``SelfIdOut`` (the signed-in Candidate's own Portal view) -- ``tests/modules/fairness/test_access.py`` scans the
OpenAPI document to prove no internal endpoint returns it."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DimensionKey = Literal["gender", "age_band", "disability"]
SelectionPoint = Literal[
    "stage_transition", "ai_match_threshold", "ai_screening_pass", "assessment_pass",
    # Epic 65 (FR-323): Phase 4 agent selection points.
    "agent_shortlist_added", "agent_screening_flag_advanced", "agent_draft_rating_accepted",
]  # fmt: skip
Intersection = Literal["gender×age_band", "gender×disability"]
AedtFeature = Literal["ai_match", "ai_screening", "assessment_autoscore", "other"]
AedtStatus = Literal["active", "retired", "inactive"]


class OptionOut(BaseModel):
    key: str
    label: str


class DimensionOut(BaseModel):
    key: DimensionKey
    label: str
    enabled: bool
    position: int
    options: list[OptionOut]


class CategorySetOut(BaseModel):
    id: uuid.UUID
    version: int
    dimensions: list[DimensionOut]


class NoticeOut(BaseModel):
    id: uuid.UUID
    version: int
    title: str
    text: str


# ---------------------------------------------------------------- settings
class FairnessSettingsOut(BaseModel):
    self_id_enabled: bool
    self_id_on_apply: bool
    self_id_in_portal: bool
    k: int
    ll144_enabled: bool
    ll144_notice_text: str | None
    alternative_process_text: str | None
    data_retention_text: str | None
    qualifications_text: str | None
    notice_lead_business_days: int
    category_set: CategorySetOut


class FairnessSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    self_id_enabled: bool | None = None
    self_id_on_apply: bool | None = None
    self_id_in_portal: bool | None = None
    k: int | None = Field(default=None, ge=5, le=100)
    ll144_enabled: bool | None = None
    ll144_notice_text: str | None = Field(default=None, max_length=10000)
    alternative_process_text: str | None = Field(default=None, max_length=10000)
    data_retention_text: str | None = Field(default=None, max_length=10000)
    qualifications_text: str | None = Field(default=None, max_length=10000)
    notice_lead_business_days: int | None = Field(default=None, ge=10, le=60)


class OptionIn(BaseModel):
    key: str = Field(max_length=40)
    label: str = Field(min_length=1, max_length=200)


class DimensionEdit(BaseModel):
    key: str = Field(max_length=40)
    label: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    options: list[OptionIn] = Field(default_factory=list, max_length=20)


class CategorySetIn(BaseModel):
    dimensions: list[DimensionEdit] = Field(max_length=3)


# ---------------------------------------------------------------- self-ID
class SelfIdOut(BaseModel):
    """The signed-in Candidate's own answers (Candidate Portal only)."""

    enabled: bool
    notice: NoticeOut | None
    dimensions: list[DimensionOut]
    answers: dict[str, str] | None


class SelfIdIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notice_id: uuid.UUID
    answers: dict[str, str] = Field(default_factory=dict, max_length=3)


class SelfIdFormOut(BaseModel):
    enabled: bool
    notice: NoticeOut | None
    dimensions: list[DimensionOut]


class PublicSelfIdIn(SelfIdIn):
    apply_token: str = Field(min_length=20, max_length=200)


class ReceivedOut(BaseModel):
    status: Literal["received"] = "received"


# ---------------------------------------------------------------- runs
class RunParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_stage_key: str | None = Field(default=None, max_length=50)
    to_stage_key: str | None = Field(default=None, max_length=50)
    min_score: float | None = Field(default=None, ge=0, le=100)
    assessment_id: uuid.UUID | None = None
    # Epic 65: ``agent_screening_flag_advanced`` source and ``agent_draft_rating_accepted`` minimum accepted share.
    source: Literal["text", "voice", "both"] | None = None
    min_accepted_share: float | None = Field(default=None, ge=0, le=1)


class RunFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    demand_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    client_id: uuid.UUID | None = None
    job_category: str | None = Field(default=None, max_length=200)


class SelectionPointOut(BaseModel):
    """Epic 65: a selection point of the run-request form (``available=false`` -> hidden)."""

    key: str
    label: str
    definition: str
    available: bool


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_point: SelectionPoint
    params: RunParams = Field(default_factory=RunParams)
    date_from: date
    date_to: date
    filters: RunFilters = Field(default_factory=RunFilters)
    intersections: list[Intersection] = Field(default_factory=lambda: ["gender×age_band"], max_length=2)  # type: ignore[arg-type]

    @model_validator(mode="after")
    def _check(self) -> "RunCreate":
        if self.date_from > self.date_to:
            msg = "date_from must not be after date_to"
            raise ValueError(msg)
        if (self.date_to - self.date_from).days > 3 * 366:
            msg = "the date range is limited to three years"
            raise ValueError(msg)
        if self.selection_point == "stage_transition" and not self.params.to_stage_key:
            msg = "to_stage_key is required for a stage transition"
            raise ValueError(msg)
        if self.selection_point == "ai_match_threshold" and self.params.min_score is None:
            msg = "min_score is required for the AI Match threshold"
            raise ValueError(msg)
        return self


class PackFileOut(BaseModel):
    name: str
    kind: Literal["pdf", "csv"]
    url: str


class PackOut(BaseModel):
    run_id: uuid.UUID
    aedt_id: uuid.UUID
    generated_at: datetime
    expires_at: datetime
    files: list[PackFileOut]


class PackIn(BaseModel):
    aedt_id: uuid.UUID


class RunSummaryOut(BaseModel):
    id: uuid.UUID
    selection_point: SelectionPoint
    params: dict[str, Any]
    date_from: date
    date_to: date
    k: int
    method: str
    status: str
    total_applicants: int | None
    created_at: datetime
    completed_at: datetime | None
    error_code: str | None


class RunOut(RunSummaryOut):
    filters: dict[str, Any]
    intersections: list[str]
    snapshot: dict[str, Any] | None
    unknown_counts: dict[str, int] | None
    pack: PackOut | None = None


# ---------------------------------------------------------------- AEDT inventory
class AedtIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature: AedtFeature
    name: str = Field(min_length=1, max_length=200)
    version: str | None = Field(default=None, max_length=60)
    first_used_on: date | None = None
    data_used: str | None = Field(default=None, max_length=5000)
    job_categories: list[str] = Field(default_factory=list, max_length=100)
    used_to_filter: bool = False
    status: AedtStatus = "inactive"
    notes: str | None = Field(default=None, max_length=5000)


class AedtPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature: AedtFeature | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    version: str | None = Field(default=None, max_length=60)
    first_used_on: date | None = None
    data_used: str | None = Field(default=None, max_length=5000)
    job_categories: list[str] | None = Field(default=None, max_length=100)
    used_to_filter: bool | None = None
    status: AedtStatus | None = None
    notes: str | None = Field(default=None, max_length=5000)


class AedtOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    feature: str
    name: str
    version: str | None
    first_used_on: date | None
    data_used: str | None
    job_categories: list[str]
    used_to_filter: bool
    status: str
    notes: str | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------- publications
class PublicationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    aedt_id: uuid.UUID
    audit_date: date
    distribution_date: date

    @model_validator(mode="after")
    def _dates(self) -> "PublicationIn":
        if self.audit_date > self.distribution_date:
            msg = "the distribution date cannot be before the audit date"
            raise ValueError(msg)
        return self


class PublicationOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    aedt_id: uuid.UUID
    aedt_name: str
    audit_date: date
    distribution_date: date
    summary: dict[str, Any]
    published_at: datetime
    unpublished_at: datetime | None


class PublicPublicationOut(BaseModel):
    id: uuid.UUID
    aedt_name: str
    feature: str
    audit_date: date
    distribution_date: date
    summary: dict[str, Any]


# ---------------------------------------------------------------- LL144
class Ll144FlagIn(BaseModel):
    flagged: bool


class Ll144FlagOut(BaseModel):
    demand_id: uuid.UUID
    flagged: bool
    jurisdiction: Literal["nyc"] | None
    notice_published_at: datetime | None
    notice_period_ends_at: datetime | None
    in_notice_period: bool
    ll144_enabled: bool


class PublicAedtOut(BaseModel):
    name: str
    feature: str
    version: str | None
    data_used: str | None


class PublicNoticeOut(BaseModel):
    applies: bool
    notice_text: str | None = None
    qualifications_text: str | None = None
    data_retention_text: str | None = None
    alternative_process_text: str | None = None
    aedts: list[PublicAedtOut] = Field(default_factory=list)


class AlternativeRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: uuid.UUID
    note: str | None = Field(default=None, max_length=2000)


class PublicAlternativeRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apply_token: str = Field(min_length=20, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class AlternativeRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    application_id: uuid.UUID
    demand_id: uuid.UUID
    channel: str
    note: str | None
    task_id: uuid.UUID | None
    requested_at: datetime
    status: str
