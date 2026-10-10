# ruff: noqa: E501
"""Submission pack contract DTOs (Epic 53 contract layer, spec 0.12; filled by Epic 61). Frozen; field sets pinned
by ``tests/contracts/signatures.json``.

``PackCheck`` / ``SubmissionPackOut`` are the frozen stub contract. Epic 61 adds the richer UI / tool shapes
(``PackCheckDetail``, ``PackDetailOut``, ``PackContextOut``, rules, metrics) below them.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PackCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    ok: bool
    message: str


class SubmissionPackOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    pack_id: uuid.UUID
    application_id: uuid.UUID
    demand_id: uuid.UUID
    client_id: uuid.UUID
    status: Literal["draft", "ready", "blocked"]
    checks: tuple[PackCheck, ...]
    document_ids: tuple[uuid.UUID, ...]
    summary_text: str
    missing: tuple[str, ...]


# ================================================================ Epic 61
CheckKey = Literal[
    "consent", "margin", "rate_card", "duplicate", "vendor_ownership", "required_fields", "resume_format"
]
CheckLevel = Literal["pass", "warn", "block", "needs_input", "unavailable"]
RateUnit = Literal["hour", "day", "month", "year"]
ResumeKind = Literal["original", "client_formatted"]
ResumeFormat = Literal["either", "original", "client_formatted"]
PackStatus = Literal["draft", "ready", "blocked"]
Lifecycle = Literal["open", "used", "discarded", "stale"]
WriteUpStatus = Literal["pending", "drafted", "failed", "edited"]
CHECK_KEYS: tuple[str, ...] = (
    "consent", "required_fields", "resume_format", "duplicate", "vendor_ownership", "margin", "rate_card",
)  # fmt: skip


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PackLink(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    id: uuid.UUID | None = None
    path: str | None = None


class PackCheckDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: CheckKey
    level: CheckLevel
    code: str
    message: str = Field(max_length=300)
    details: dict[str, Any] = Field(default_factory=dict)
    link: PackLink | None = None


class PackRatesIn(_In):
    pay_rate: Decimal | None = Field(default=None, ge=Decimal(0), max_digits=14, decimal_places=2)
    bill_rate: Decimal | None = Field(default=None, ge=Decimal(0), max_digits=14, decimal_places=2)
    pay_rate_unit: RateUnit | None = None
    bill_rate_unit: RateUnit | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class PackDetailOut(BaseModel):
    id: uuid.UUID
    human_id: str
    application_id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_name: str | None = None
    demand_id: uuid.UUID
    demand_title: str | None = None
    client_id: uuid.UUID
    client_name: str | None = None
    status: PackStatus
    lifecycle: Lifecycle
    version: int
    resume_kind: ResumeKind | None
    resume_id: uuid.UUID | None
    resume_version: int | None
    resume_uploaded_at: datetime | None = None
    resume_stale: bool = False
    document_ids: list[uuid.UUID]
    summary_text: str | None
    write_up_status: WriteUpStatus
    word_count: int
    max_words: int
    pay_rate: str | None
    bill_rate: str | None
    currency: str | None
    pay_rate_unit: RateUnit | None
    bill_rate_unit: RateUnit | None
    rates_hidden: bool
    email_subject: str | None
    checks: list[PackCheckDetail]
    missing: list[str]
    blocker_count: int
    warning_count: int
    checked_at: datetime | None
    consent_request_sent_at: datetime | None
    prepared_by_kind: str
    prepared_by_id: uuid.UUID | None
    agent_run_id: uuid.UUID | None
    opened_in_dialog_at: datetime | None
    submission_id: uuid.UUID | None
    submission_human_id: str | None = None
    used_at: datetime | None
    user_changes: dict[str, Any] | None
    discarded_reason: str | None
    lock_version: int
    created_at: datetime
    updated_at: datetime


class PackSummaryOut(BaseModel):
    id: uuid.UUID
    human_id: str
    application_id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_name: str | None
    demand_id: uuid.UUID
    demand_title: str | None
    client_id: uuid.UUID
    client_name: str | None
    status: PackStatus
    lifecycle: Lifecycle
    blocker_count: int
    warning_count: int
    prepared_by_kind: str
    owner_id: uuid.UUID | None
    owner_name: str | None
    created_at: datetime
    updated_at: datetime


class PackPage(BaseModel):
    items: list[PackSummaryOut]
    total: int
    page: int
    page_size: int


class PackPatchIn(_In):
    summary_text: str | None = Field(default=None, max_length=3000)
    rates: PackRatesIn | None = None
    email_subject: str | None = Field(default=None, max_length=300)
    lock_version: int = Field(ge=1)


class PackDiscardIn(_In):
    reason: str = Field(default="user_discarded", min_length=3, max_length=40, pattern=r"^[a-z_]+$")


class PackChecksIn(_In):
    rates: PackRatesIn | None = None


class PackChecksOut(BaseModel):
    checks: list[PackCheckDetail]


class ClientSubmissionRulesIn(_In):
    resume_format: ResumeFormat = "either"
    mask_contact_details: bool = True
    write_up_max_words: int = Field(default=150, ge=40, le=400)
    write_up_instructions: str | None = Field(default=None, max_length=1000)
    duplicate_window_days: int | None = Field(default=None, ge=1, le=730)
    require_candidate_consent: bool = False
    show_notice_period: bool = True


class ClientSubmissionRulesOut(BaseModel):
    client_id: uuid.UUID
    resume_format: ResumeFormat
    mask_contact_details: bool
    write_up_max_words: int
    write_up_instructions: str | None
    duplicate_window_days: int | None
    require_candidate_consent: bool
    show_notice_period: bool
    is_default: bool
    tenant_duplicate_window_days: int
    tenant_consent_mode: str
    updated_by: uuid.UUID | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------- read tool context
class ContextCandidate(BaseModel):
    candidate_id: uuid.UUID
    current_title: str | None
    total_experience_years: float | None
    location: str | None
    notice_period_days: int | None
    availability_date: date | None
    skills: list[str]
    inferred_skills: list[str]
    expected_pay: str | None
    expected_pay_unit: str | None
    expected_pay_currency: str | None


class ContextResume(BaseModel):
    resume_id: uuid.UUID
    version: int | None
    parse_status: str
    uploaded_at: datetime


class ContextScreening(BaseModel):
    kind: str
    outcome: str | None
    summary: str | None


class ContextInterview(BaseModel):
    stage: str | None
    recommendation: str | None
    feedback_summary: str | None


class ContextMatch(BaseModel):
    explanation: str | None


class ContextDemand(BaseModel):
    demand_id: uuid.UUID
    title: str
    client_id: uuid.UUID
    location: str | None
    employment_type: str | None
    bill_rate: str | None
    pay_rate_max: str | None
    currency: str | None
    rate_unit: str | None
    submission_limit: int | None


class ContextRules(BaseModel):
    resume_format: ResumeFormat
    mask_contact_details: bool
    write_up_max_words: int
    write_up_instructions: str | None
    duplicate_window_days: int
    require_candidate_consent: bool
    show_notice_period: bool
    consent_mode: str


class ContextPack(BaseModel):
    pack_id: uuid.UUID
    human_id: str
    status: PackStatus
    write_up_status: WriteUpStatus
    consent_request_sent_at: datetime | None
    blocker_count: int


class PackContextOut(BaseModel):
    application_id: uuid.UUID
    stage: str
    shortlisted: bool
    candidate: ContextCandidate
    resumes: list[ContextResume]
    screening: list[ContextScreening]
    interviews: list[ContextInterview]
    match: ContextMatch
    demand: ContextDemand
    rules: ContextRules
    prior_submissions_to_client: int
    open_pack: ContextPack | None


class PackAgentStatusOut(BaseModel):
    enabled: bool
    paused: bool


class PackMetricsOut(BaseModel):
    period_from: date
    period_to: date
    submissions_with_pack: int
    submissions_without_pack: int
    median_hours_with_pack: float | None
    median_hours_without_pack: float | None
    packs_used: int
    packs_low_change: int
    pct_low_change: float | None
    open_packs: int
    blocked_packs: int


__all__ = [
    "CHECK_KEYS",
    "CheckKey",
    "CheckLevel",
    "ClientSubmissionRulesIn",
    "ClientSubmissionRulesOut",
    "ContextCandidate",
    "ContextDemand",
    "ContextInterview",
    "ContextMatch",
    "ContextPack",
    "ContextResume",
    "ContextRules",
    "ContextScreening",
    "PackAgentStatusOut",
    "PackCheck",
    "PackCheckDetail",
    "PackChecksIn",
    "PackChecksOut",
    "PackContextOut",
    "PackDetailOut",
    "PackDiscardIn",
    "PackLink",
    "PackMetricsOut",
    "PackPage",
    "PackPatchIn",
    "PackRatesIn",
    "PackSummaryOut",
    "SubmissionPackOut",
]
