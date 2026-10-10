# ruff: noqa: E501
"""Job board distribution DTOs (Epic 70, spec A8)."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

BoardKey = Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_]{1,39}$")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MultiPostIn(_In):
    board_keys: list[str] | None = Field(default=None, max_length=10)
    board_set_id: uuid.UUID | None = None
    board_fields: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PublishIn(MultiPostIn):
    acknowledge_bias_flags: bool = False


class FieldOptionOut(_Out):
    value: str
    label: str


class BoardFieldOut(_Out):
    key: str
    label: str
    required: bool
    kind: str
    options: list[FieldOptionOut] = Field(default_factory=list)


class BoardPreviewOut(_Out):
    board_key: str
    label: str
    installation_id: uuid.UUID | None
    title: str
    company: str | None
    location: str | None
    experience: str | None
    salary: str | None  # None = hidden
    apply_url: str
    description_excerpt: str
    fields: list[BoardFieldOut]
    values: dict[str, Any]
    blocked_fields: list[str]
    blocked_labels: list[str]
    credits_left: int | None
    will_publish: bool
    error_code: str | None = None


class BiasMatchOut(_Out):
    term: str
    category: str
    suggestion: str | None = None


class MultiPostPreviewOut(_Out):
    demand_id: uuid.UUID
    posting_id: uuid.UUID
    bias_matches: list[dict[str, Any]]
    boards: list[BoardPreviewOut]


class BoardPostingOut(_Out):
    id: uuid.UUID
    human_id: str
    demand_id: uuid.UUID
    posting_id: uuid.UUID
    installation_id: uuid.UUID
    board_key: str
    board_label: str = ""
    status: str
    revision: int
    local_revision: int
    out_of_date: bool = False
    board_ref: str | None
    board_url: str | None
    expires_at: datetime | None
    views: int | None
    applies_reported: int | None
    blocked_fields: list[str]
    last_status_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    manual: bool
    published_by: uuid.UUID | None
    published_at: datetime | None
    closed_at: datetime | None
    close_reason: str | None
    cost_note: str | None


class PublishOut(_Out):
    published: list[BoardPostingOut]
    blocked: list[BoardPreviewOut]


class IdsIn(_In):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=50)


class RecordIn(_In):
    board_ref: str = Field(min_length=1, max_length=200)
    board_url: str | None = Field(default=None, max_length=2000, pattern=r"^https?://")
    posted_on: date
    expires_on: date | None = None
    cost_note: str | None = Field(default=None, max_length=200)


class PackFieldOut(_Out):
    label: str
    value: str


class PackOut(_Out):
    board_key: str
    title: str
    description_text: str
    description_html: str
    fields: list[PackFieldOut]
    apply_url: str


class ApplicantRowOut(_Out):
    id: uuid.UUID
    status: str
    suppression_reason: str | None
    candidate_id: uuid.UUID | None
    application_id: uuid.UUID | None
    created_candidate: bool
    resume_status: str | None
    error_code: str | None
    received_at: datetime


class JobBoardSettingsIn(_In):
    expiry_warning_days: int | None = Field(default=None, ge=1, le=14)
    applicant_digest: bool | None = None
    auto_close_on_demand_change: bool | None = None
    default_board_set_id: uuid.UUID | None = None


class JobBoardSettingsOut(_Out):
    expiry_warning_days: int
    applicant_digest: bool
    auto_close_on_demand_change: bool
    default_board_set_id: uuid.UUID | None


EmploymentType = Literal["contract", "contract_to_hire", "permanent", "freelance"]


class BoardSetIn(_In):
    name: str = Field(min_length=1, max_length=120)
    employment_type: EmploymentType | None = None
    board_keys: list[str] = Field(default_factory=list, max_length=10)
    is_default: bool = False


class BoardSetPatch(_In):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    board_keys: list[str] | None = Field(default=None, max_length=10)
    is_default: bool | None = None


class BoardSetOut(_Out):
    id: uuid.UUID
    name: str
    employment_type: str | None
    board_keys: list[str]
    is_default: bool


MappingKind = Literal["function", "industry", "location", "experience_band", "salary_band", "employment_type", "education", "default"]


class MappingIn(_In):
    kind: MappingKind
    local_value: str = Field(min_length=1, max_length=200)
    board_value: str = Field(min_length=1, max_length=200)
    board_label: str | None = Field(default=None, max_length=200)


class MappingsPut(_In):
    items: list[MappingIn] = Field(max_length=2000)


class MappingOut(_Out):
    id: uuid.UUID
    board_key: str
    kind: str
    local_value: str
    board_value: str
    board_label: str | None


class FeedSettingsIn(_In):
    enabled: bool | None = None
    include_salary: bool | None = None
    company_name_mode: Literal["tenant", "client"] | None = None


class FeedSettingsOut(_Out):
    enabled: bool
    include_salary: bool
    company_name_mode: str
    feed_url: str


class BoardPostingReportRow(_Out):
    board_posting_id: uuid.UUID
    human_id: str
    demand_id: uuid.UUID
    title: str
    status: str
    applications: int
    submissions: int
    interviews: int
    offers: int
    placements: int
    cost_note: str | None


class BoardReportRow(_Out):
    board_key: str
    label: str
    postings: int
    applications: int
    submissions: int
    interviews: int
    offers: int
    placements: int
    cost_note: str | None
    rows: list[BoardPostingReportRow]


class ReportQuery(_In):
    from_: date | None = Field(default=None, alias="from")
    to: date | None = None
    board_key: str | None = None
    demand_id: uuid.UUID | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
