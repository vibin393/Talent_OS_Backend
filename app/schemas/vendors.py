"""Vendor contract DTOs (Epic 13 contract layer; filled by Epic 17) and the internal (tenant-user) API models
of
the vendors domain. Vendor Portal models live in ``app/schemas/vendors_portal.py`` and never share a class
with
these (FR-80)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.money import DecimalStr


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class VendorSummary(_Frozen):
    id: uuid.UUID
    human_id: str
    legal_name: str
    status: str


class VendorSourcing(_Frozen):
    vendor_id: uuid.UUID
    vendor_submission_id: uuid.UUID | None
    vendor_rate: DecimalStr | None
    rate_unit: str | None
    currency: str | None


# ================================================================ internal API (Epic 17)
class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


VendorStatus = Literal["prospective", "onboarding", "active", "suspended", "inactive"]
VendorTier = Literal["gold", "silver", "bronze", "custom"]
DocCategory = Literal["agreement", "insurance", "registration", "other"]
RateUnit = Literal["hour", "day", "month", "year", "fixed"]
Str200 = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Rate = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=4)]


class ContactIn(_In):
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=30)


class VendorCreate(_In):
    legal_name: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    pan: str | None = Field(default=None, pattern=r"^[A-Za-z]{5}[0-9]{4}[A-Za-z]$")
    address: dict[str, Any] = Field(default_factory=dict)
    primary_contact: ContactIn = Field(default_factory=ContactIn)
    services: list[Str200] = Field(default_factory=list, max_length=50)
    skill_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    regions: list[Str200] = Field(default_factory=list, max_length=50)
    tier: VendorTier | None = None
    owner_id: uuid.UUID | None = None


class VendorUpdate(_In):
    legal_name: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None = None
    pan: str | None = Field(default=None, pattern=r"^[A-Za-z]{5}[0-9]{4}[A-Za-z]$")
    address: dict[str, Any] | None = None
    primary_contact: ContactIn | None = None
    services: list[Str200] | None = Field(default=None, max_length=50)
    skill_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)
    regions: list[Str200] | None = Field(default=None, max_length=50)
    tier: VendorTier | None = None
    owner_id: uuid.UUID | None = None


class VendorTransitionIn(_In):
    to: VendorStatus
    reason: str | None = Field(default=None, max_length=2000)


class GstinIn(_In):
    gstin: Annotated[str, StringConstraints(min_length=15, max_length=15, to_upper=True)]
    state_code: Annotated[str, StringConstraints(min_length=2, max_length=2)]


class GstinOut(BaseModel):
    id: uuid.UUID
    gstin: str
    state_code: str


class BankAccountIn(_In):
    account_name: Str200
    account_no: Annotated[str, StringConstraints(min_length=6, max_length=34)]
    ifsc: Annotated[str, StringConstraints(pattern=r"^[A-Za-z]{4}0[A-Za-z0-9]{6}$", to_upper=True)]
    is_primary: bool = True


class BankAccountOut(BaseModel):
    id: uuid.UUID
    account_name: str
    account_no_last4: str
    ifsc: str
    is_primary: bool
    status: str
    approval_request_id: uuid.UUID | None


class RevealIn(_In):
    field: Literal["pan", "bank_account"]
    bank_account_id: uuid.UUID | None = None


class RevealOut(BaseModel):
    field: str
    value: str


class VendorDocumentOut(BaseModel):
    id: uuid.UUID
    human_id: str
    category: str
    title: str
    expires_on: date | None
    status: Literal["valid", "expiring", "expired", "no_expiry"]
    current_version_id: uuid.UUID | None
    pending_review: bool = False
    created_at: datetime


class VendorOut(BaseModel):
    id: uuid.UUID
    human_id: str
    legal_name: str
    status: VendorStatus
    tier: str | None
    pan_last4: str | None
    address: dict[str, Any]
    primary_contact: dict[str, Any]
    services: list[str]
    skill_ids: list[uuid.UUID]
    regions: list[str]
    owner_id: uuid.UUID | None
    suspended_reason: str | None
    inactive_reason: str | None
    created_at: datetime
    updated_at: datetime


class VendorDetailOut(VendorOut):
    gstins: list[GstinOut] = Field(default_factory=list)
    bank_accounts: list[BankAccountOut] = Field(default_factory=list)
    activation_blockers: list[str] = Field(default_factory=list)
    agreement_active: bool = False


class VendorSettingsOut(BaseModel):
    ownership_window_days: int
    duplicate_activity_months: int
    suspension_grace_days: int
    mandatory_document_kinds: list[str]
    scorecard_visible_to_vendor: bool
    vendor_labels: dict[str, str]
    notice_link_days: int
    require_bench_review_for_vendor_release: bool


class VendorSettingsUpdate(_In):
    ownership_window_days: int | None = Field(default=None, ge=1, le=730)
    duplicate_activity_months: int | None = Field(default=None, ge=0, le=60)
    suspension_grace_days: int | None = Field(default=None, ge=0, le=365)
    mandatory_document_kinds: list[DocCategory] | None = None
    scorecard_visible_to_vendor: bool | None = None
    vendor_labels: dict[str, Annotated[str, StringConstraints(min_length=1, max_length=60)]] | None = None
    notice_link_days: int | None = Field(default=None, ge=1, le=365)
    require_bench_review_for_vendor_release: bool | None = None


# ---------------------------------------------------------------- releases
class ReleaseCreate(_In):
    vendor_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    tier: VendorTier | None = None
    reveal_client: bool = False
    max_vendor_rate: Rate | None = None
    rate_unit: RateUnit | None = None
    currency: Annotated[str, StringConstraints(min_length=3, max_length=3, to_upper=True)] | None = None
    submission_limit: int | None = Field(default=None, ge=1, le=1000)
    deadline_at: datetime | None = None
    instructions: str | None = Field(default=None, max_length=5000)


class ReleaseOut(BaseModel):
    id: uuid.UUID
    human_id: str
    demand_id: uuid.UUID
    vendor_id: uuid.UUID
    vendor_name: str | None = None
    status: str
    reveal_client: bool
    max_vendor_rate: DecimalStr | None
    rate_unit: str | None
    currency: str
    submission_limit: int | None
    submissions_count: int = 0
    deadline_at: datetime | None
    instructions: str | None
    released_at: datetime
    paused_by_system: bool
    title: str


class ReleaseItemResult(BaseModel):
    vendor_id: uuid.UUID
    status: Literal["released", "skipped"]
    reason: str | None = None
    release: ReleaseOut | None = None


class ReleaseBatchOut(BaseModel):
    items: list[ReleaseItemResult]


# ---------------------------------------------------------------- submissions (internal review)
class DuplicateFinding(BaseModel):
    rule: str
    category: str
    detail: str
    candidate_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    submission_id: uuid.UUID | None = None


class MarginPreviewOut(BaseModel):
    bill_rate: DecimalStr | None
    pay_rate: DecimalStr
    margin_amount: DecimalStr | None
    margin_pct: DecimalStr | None
    currency: str


class VendorSubmissionOut(BaseModel):
    id: uuid.UUID
    human_id: str
    vendor_release_id: uuid.UUID
    vendor_id: uuid.UUID
    vendor_name: str | None = None
    demand_id: uuid.UUID
    demand_title: str | None = None
    client_id: uuid.UUID
    full_name: str | None
    email: str | None
    phone_e164: str | None
    current_location: str | None
    total_experience_months: int | None
    availability_date: date | None
    notice_period_days: int | None
    vendor_rate: DecimalStr
    rate_unit: str | None
    currency: str
    max_vendor_rate: DecimalStr | None = None
    status: str
    duplicate_reason_category: str | None
    reject_reason_code: str | None
    reason_text_internal: str | None
    duplicate_findings: list[DuplicateFinding] = Field(default_factory=list)
    candidate_id: uuid.UUID | None
    application_id: uuid.UUID | None
    progress_label: str | None
    resume_file_id: uuid.UUID | None
    notice_consent: str | None
    margin: MarginPreviewOut | None = None
    review_due_at: datetime | None
    reviewed_at: datetime | None
    created_at: datetime


class AcceptIn(_In):
    link_candidate_id: uuid.UUID | None = None
    create: bool = False
    override_duplicate_rule: bool = False
    reason: str | None = Field(default=None, max_length=2000)


class RejectIn(_In):
    reason_code: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    note: str | None = Field(default=None, max_length=4000)


class MarkDuplicateIn(_In):
    category: Literal["owned_by_other_vendor", "existing_candidate", "submitted_by_other_vendor", "other"]
    note: str | None = Field(default=None, max_length=4000)


class ReopenIn(_In):
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2000)]


# ---------------------------------------------------------------- performance
class VendorScorecard(BaseModel):
    vendor_id: uuid.UUID
    vendor_name: str | None = None
    period_from: date
    period_to: date
    releases_received: int
    submissions: int
    accepted: int
    acceptance_pct: DecimalStr | None
    duplicates: int
    duplicate_pct: DecimalStr | None
    interviews: int
    submission_to_interview_pct: DecimalStr | None
    offers: int
    interview_to_offer_pct: DecimalStr | None
    placements: int
    median_hours_to_first_submission: DecimalStr | None
    sla_breaches: int
    early_exits: int


# ---------------------------------------------------------------- statements
class StatementBuildIn(_In):
    vendor_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)
    all: bool = False
    legal_entity_id: uuid.UUID
    period_start: date
    period_end: date


class StatementLineOut(BaseModel):
    id: uuid.UUID
    line_kind: str
    source_kind: str
    source_id: uuid.UUID
    placement_id: uuid.UUID | None
    worker_name_snapshot: str | None
    work_date: date
    time_category: str | None
    quantity: DecimalStr
    vendor_rate: DecimalStr | None
    multiplier: DecimalStr
    amount: DecimalStr
    currency: str


class StatementOut(BaseModel):
    id: uuid.UUID
    human_id: str
    vendor_id: uuid.UUID
    vendor_name: str | None = None
    legal_entity_id: uuid.UUID
    period_start: date
    period_end: date
    status: str
    currency: str
    total_amount: DecimalStr
    content_hash: str | None
    finalized_at: datetime | None
    line_count: int = 0
    created_at: datetime


class StatementDetailOut(StatementOut):
    lines: list[StatementLineOut] = Field(default_factory=list)


class StatementBuildOut(BaseModel):
    statements: list[StatementOut]
    skipped: list[dict[str, Any]] = Field(default_factory=list)
