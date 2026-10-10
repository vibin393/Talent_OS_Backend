"""Recruitment marketing DTOs (Epic 39): settings, audiences, campaigns, sequences, enrolments, analytics."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Channel = Literal["email", "sms", "whatsapp"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
HHMM = Annotated[str, StringConstraints(pattern=r"^[0-2][0-9]:[0-5][0-9]$")]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ================================================================ settings
class SendWindow(BaseModel):
    start: HHMM
    end: HHMM


class ChannelNumbers(BaseModel):
    email: int | None = Field(default=None, ge=1)
    sms: int | None = Field(default=None, ge=1)
    whatsapp: int | None = Field(default=None, ge=1)


class MarketingSettingsOut(_Out):
    frequency_cap_count: int
    frequency_cap_days: int
    send_windows: dict[str, SendWindow]
    approval_threshold: int
    ab_window_hours: int
    rate_per_minute: dict[str, int]
    daily_cap: dict[str, int]
    platform_max_rate: dict[str, int]
    platform_max_daily: dict[str, int]
    attribution_window_days: int
    default_timezone: str | None
    footer_contact: str | None
    paused_at: datetime | None
    paused_reason: str | None


class MarketingSettingsUpdate(_In):
    frequency_cap_count: int | None = Field(default=None, ge=1, le=3)
    send_windows: dict[Channel, SendWindow] | None = None
    approval_threshold: int | None = Field(default=None, ge=1, le=10_000_000)
    ab_window_hours: int | None = Field(default=None, ge=1, le=72)
    rate_per_minute: ChannelNumbers | None = None
    daily_cap: ChannelNumbers | None = None
    default_timezone: Annotated[str, StringConstraints(max_length=64)] | None = None
    footer_contact: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


class ReasonIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


# ================================================================ audiences
class AudienceSourceIn(_In):
    mode: Literal["include", "exclude"] = "include"
    kind: Literal["pool", "segment"]
    talent_pool_id: uuid.UUID | None = None
    segment_id: uuid.UUID | None = None


class AudienceSourceOut(_Out):
    id: uuid.UUID
    mode: str
    kind: str
    talent_pool_id: uuid.UUID | None
    segment_id: uuid.UUID | None
    name: str | None = None


class AudienceCreate(_In):
    name: Name
    description: Description | None = None
    sources: list[AudienceSourceIn] = Field(default_factory=list, max_length=50)


class AudienceUpdate(_In):
    name: Name | None = None
    description: Description | None = None
    status: Literal["draft", "active"] | None = None
    sources: list[AudienceSourceIn] | None = Field(default=None, max_length=50)


class AudienceOut(_Out):
    id: uuid.UUID
    name: str
    description: str | None
    owner_id: uuid.UUID
    status: str
    version: int
    sources: list[AudienceSourceOut]
    include_list_count: int
    exclude_list_count: int
    created_at: datetime
    updated_at: datetime


class ListEntriesIn(_In):
    mode: Literal["include", "exclude"] = "include"
    candidate_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)


class ListImportRow(BaseModel):
    row: int
    value: str
    candidate_id: uuid.UUID | None = None


class ListImportOut(BaseModel):
    mode: str
    applied: bool
    matched: int
    unmatched: int
    matched_rows: list[ListImportRow]
    unmatched_rows: list[ListImportRow]


class AudiencePreviewOut(BaseModel):
    audience_id: uuid.UUID
    total: int
    reachable: dict[str, int]
    suppressed: dict[str, dict[str, int]]
    elapsed_ms: int


# ================================================================ campaigns
class JobLinkIn(_In):
    posting_id: uuid.UUID
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = (
        None
    )


class JobLinkOut(BaseModel):
    index: int
    posting_id: uuid.UUID
    demand_id: uuid.UUID | None = None
    label: str
    url: str


class AbVariant(BaseModel):
    key: Literal["A", "B"]
    subject: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class AbTestIn(_In):
    variants: list[AbVariant] = Field(min_length=2, max_length=2)
    sample_pct: int = Field(ge=2, le=20)
    metric: Literal["open", "click"] = "open"
    window_hours: int | None = Field(default=None, ge=1, le=72)


class AbTestOut(BaseModel):
    variants: list[AbVariant]
    sample_pct: int
    metric: str
    window_hours: int
    winner_key: str | None = None
    decided_at: datetime | None = None


class CampaignCreate(_In):
    name: Name
    channel: Channel
    audience_id: uuid.UUID
    email_subject: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    email_body_html: Annotated[str, StringConstraints(max_length=200_000)] | None = None
    email_template_id: uuid.UUID | None = None
    channel_template_id: uuid.UUID | None = None
    job_links: list[JobLinkIn] = Field(default_factory=list, max_length=10)
    ab_test: AbTestIn | None = None


class CampaignUpdate(_In):
    name: Name | None = None
    audience_id: uuid.UUID | None = None
    email_subject: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    email_body_html: Annotated[str, StringConstraints(max_length=200_000)] | None = None
    email_template_id: uuid.UUID | None = None
    channel_template_id: uuid.UUID | None = None
    job_links: list[JobLinkIn] | None = Field(default=None, max_length=10)
    ab_test: AbTestIn | None = None
    clear_ab_test: bool = False


class CampaignSubmitIn(_In):
    scheduled_at: datetime | None = None


class CampaignOut(_Out):
    id: uuid.UUID
    name: str
    channel: str
    audience_id: uuid.UUID
    audience_name: str | None = None
    snapshot_id: uuid.UUID | None
    owner_id: uuid.UUID
    status: str
    scheduled_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    approval_request_id: uuid.UUID | None
    email_subject: str | None
    email_body_html: str | None
    email_template_id: uuid.UUID | None
    channel_template_id: uuid.UUID | None
    job_links: list[JobLinkOut]
    ab_test: AbTestOut | None
    counts: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


class TestSendOut(BaseModel):
    channel: str
    queued: bool
    message: str


class CampaignMessageOut(BaseModel):
    id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_name: str | None
    channel: str
    status: str
    suppression_reason: str | None
    variant_key: str | None
    sent_at: datetime | None
    defer_until: datetime | None


class CampaignMessagesPage(BaseModel):
    items: list[CampaignMessageOut]
    total: int
    page: int
    page_size: int


# ================================================================ analytics
class FunnelCounts(BaseModel):
    audience: int = 0
    suppressed: int = 0
    sent: int = 0
    delivered: int = 0
    bounced: int = 0
    opened: int = 0
    clicked: int = 0
    replied: int = 0
    unsubscribed: int = 0
    applied: int = 0
    hired: int = 0


class FunnelRates(BaseModel):
    delivery_rate: float = 0.0
    open_rate: float = 0.0
    click_rate: float = 0.0
    reply_rate: float = 0.0
    unsubscribe_rate: float = 0.0
    apply_rate: float = 0.0
    hire_rate: float = 0.0


class FunnelSlice(BaseModel):
    key: str
    label: str
    counts: FunnelCounts
    rates: FunnelRates


class FunnelOut(BaseModel):
    counts: FunnelCounts
    rates: FunnelRates
    suppressed_by_reason: dict[str, int]
    variants: list[FunnelSlice] = Field(default_factory=list)
    steps: list[FunnelSlice] = Field(default_factory=list)
    opens_approximate: bool = True


# ================================================================ sequences
class StepIn(_In):
    kind: Literal["send", "wait", "branch", "task", "add_to_pool"]
    config: dict[str, Any] = Field(default_factory=dict)


class StepOut(_Out):
    id: uuid.UUID
    position: int
    kind: str
    config: dict[str, Any]


class ExitOn(BaseModel):
    replied: bool = True
    applied: bool = True
    hired: bool = True
    unsubscribed: bool = True


class SequenceCreate(_In):
    name: Name
    description: Description | None = None
    basis: Literal["marketing", "own_application"] = "marketing"
    exit_on: ExitOn | None = None
    re_enrol_gap_days: int = Field(default=30, ge=0, le=365)
    steps: list[StepIn] = Field(default_factory=list, max_length=20)


class SequenceUpdate(_In):
    name: Name | None = None
    description: Description | None = None
    basis: Literal["marketing", "own_application"] | None = None
    exit_on: ExitOn | None = None
    re_enrol_gap_days: int | None = Field(default=None, ge=0, le=365)
    steps: list[StepIn] | None = Field(default=None, max_length=20)


class PublishIn(_In):
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


class SequenceOut(_Out):
    id: uuid.UUID
    name: str
    description: str | None
    owner_id: uuid.UUID
    status: str
    current_version: int
    draft_version: int
    basis: str
    exit_on: dict[str, bool]
    re_enrol_gap_days: int
    steps: list[StepOut]
    published_steps: list[StepOut]
    active_enrollments: int
    created_at: datetime
    updated_at: datetime


class EnrolIn(_In):
    candidate_ids: list[uuid.UUID] | None = Field(default=None, max_length=5000)
    audience_id: uuid.UUID | None = None


class EnrolRefusal(BaseModel):
    candidate_id: uuid.UUID
    reason: str


class EnrolOut(BaseModel):
    enrolled: int
    enrollment_ids: list[uuid.UUID]
    refused: list[EnrolRefusal]


class EnrollmentOut(BaseModel):
    id: uuid.UUID
    sequence_id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_name: str | None
    version: int
    status: str
    exit_reason: str | None
    position: int
    next_run_at: datetime | None
    origin: str
    started_at: datetime
    ended_at: datetime | None


# ================================================================ pickers
class Option(BaseModel):
    id: uuid.UUID
    name: str
    detail: str | None = None


class MarketingOptionsOut(BaseModel):
    pools: list[Option]
    segments: list[Option]
    sms_templates: list[Option]
    whatsapp_templates: list[Option]
    postings: list[Option]
    sequences: list[Option]
