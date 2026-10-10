# ruff: noqa: E501
"""Internal referral schemas (Epic 42): programs, settings, the full referral record (``referral:read``), rewards,
payout export and the public referee page. Referrer-facing payloads live in ``schemas/referrals_referrer.py``."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.core.money import DecimalStr

SeniorityLevel = Literal["junior", "mid", "senior"]
EligibleReferrers = Literal["users", "workers", "both"]
ReferralStatus = Literal[
    "submitted",
    "accepted",
    "in_process",
    "hired",
    "not_selected",
    "duplicate",
    "declined",
    "expired",
    "withdrawn",
]
RewardStatus = Literal["pending_condition", "eligible", "approved", "paid", "void"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _ccy(v: str) -> str:
    v = (v or "").strip().upper()
    if len(v) != 3 or not v.isalpha():
        msg = "Use an ISO 4217 currency code"
        raise ValueError(msg)
    return v


# ================================================================ programs
class RewardMatch(_In):
    seniority_level: SeniorityLevel | None = None
    demand_id: uuid.UUID | None = None
    employment_type: str | None = Field(default=None, max_length=20)

    def key(self) -> tuple[str | None, str | None, str | None]:
        return (self.seniority_level, str(self.demand_id) if self.demand_id else None, self.employment_type)


class RewardRule(_In):
    match: RewardMatch
    amount: DecimalStr = Field(gt=Decimal(0), max_digits=14, decimal_places=2)
    currency: str

    _c = field_validator("currency")(_ccy)


class EligibleDemands(_In):
    all: bool = False
    client_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    demand_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    employment_types: list[str] = Field(default_factory=list, max_length=20)
    seniority_levels: list[SeniorityLevel] = Field(default_factory=list, max_length=3)


class Exclusions(_In):
    assigned_recruiters: bool = True
    demand_owner: bool = True
    excluded_role_keys: list[str] = Field(default_factory=list, max_length=20)


def _check_rules(rules: list[RewardRule]) -> list[RewardRule]:
    keys = [r.match.key() for r in rules]
    if len(keys) != len(set(keys)):
        msg = "Two reward rules have the same match"
        raise ValueError(msg)
    if any(k == (None, None, None) for k in keys):
        msg = "A reward rule needs at least one match key (use the default amount otherwise)"
        raise ValueError(msg)
    return rules


class ProgramIn(_In):
    name: str = Field(min_length=1, max_length=200)
    active: bool = True
    valid_from: date
    valid_to: date | None = None
    eligible_referrers: EligibleReferrers = "both"
    eligible_demands: EligibleDemands = Field(default_factory=lambda: EligibleDemands(all=True))
    reward_rules: list[RewardRule] = Field(default_factory=list, max_length=50)
    default_amount: DecimalStr | None = Field(default=None, gt=Decimal(0), max_digits=14, decimal_places=2)
    currency: str = "INR"
    joining_condition_days: int = Field(default=90, ge=0, le=730)
    exclusions: Exclusions = Field(default_factory=Exclusions)
    priority: int = Field(default=0, ge=-1000, le=1000)

    _c = field_validator("currency")(_ccy)
    _r = field_validator("reward_rules")(_check_rules)

    @model_validator(mode="after")
    def _validity(self) -> "ProgramIn":
        if self.valid_to is not None and self.valid_to < self.valid_from:
            msg = "valid_to must be on or after valid_from"
            raise ValueError(msg)
        return self


class ProgramPatch(_In):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    active: bool | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    eligible_referrers: EligibleReferrers | None = None
    eligible_demands: EligibleDemands | None = None
    reward_rules: list[RewardRule] | None = Field(default=None, max_length=50)
    default_amount: DecimalStr | None = Field(default=None, gt=Decimal(0), max_digits=14, decimal_places=2)
    currency: str | None = None
    joining_condition_days: int | None = Field(default=None, ge=0, le=730)
    exclusions: Exclusions | None = None
    priority: int | None = Field(default=None, ge=-1000, le=1000)

    @field_validator("currency")
    @classmethod
    def _c(cls, v: str | None) -> str | None:
        return None if v is None else _ccy(v)

    @field_validator("reward_rules")
    @classmethod
    def _r(cls, v: list[RewardRule] | None) -> list[RewardRule] | None:
        return None if v is None else _check_rules(v)


class ProgramOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    active: bool
    valid_from: date
    valid_to: date | None
    eligible_referrers: str
    eligible_demands: EligibleDemands
    reward_rules: list[RewardRule]
    default_amount: DecimalStr | None
    currency: str
    joining_condition_days: int
    exclusions: Exclusions
    priority: int
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ================================================================ settings
class SettingsIn(_In):
    duplicate_activity_months: int = Field(default=6, ge=1, le=60)
    referee_link_days: int = Field(default=14, ge=1, le=90)
    share_link_days: int = Field(default=90, ge=1, le=365)
    default_owner_user_id: uuid.UUID | None = None
    payout_export_currency_default: str = "INR"

    _c = field_validator("payout_export_currency_default")(_ccy)


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    duplicate_activity_months: int
    referee_link_days: int
    share_link_days: int
    default_owner_user_id: uuid.UUID | None
    payout_export_currency_default: str


# ================================================================ referrals (internal view)
class RewardOut(BaseModel):
    id: uuid.UUID
    referral_id: uuid.UUID
    referral_human_id: str
    referee_name: str | None
    referrer_kind: str
    referrer_name: str | None
    program_id: uuid.UUID
    placement_id: uuid.UUID
    status: RewardStatus
    amount: DecimalStr
    currency: str
    condition_due_on: date | None
    eligible_at: datetime | None
    approval_request_id: uuid.UUID | None
    approved_at: datetime | None
    payout_channel: str
    payee_placement_id: uuid.UUID | None
    paid_at: datetime | None
    payout_ref: str | None
    void_reason: str | None
    created_at: datetime


class ReferralOut(BaseModel):
    id: uuid.UUID
    human_id: str
    status: ReferralStatus
    channel: str
    demand_id: uuid.UUID | None
    demand_title: str | None
    program_id: uuid.UUID | None
    program_name: str | None
    referrer_kind: str
    referrer_user_id: uuid.UUID | None
    referrer_candidate_id: uuid.UUID | None
    referrer_name: str | None
    referee_name: str | None
    referee_email: str | None
    referee_phone: str | None
    relationship: str | None
    note: str | None
    has_resume: bool
    candidate_id: uuid.UUID | None
    application_id: uuid.UUID | None
    placement_id: uuid.UUID | None
    reward_eligible: bool
    ineligible_reason: str | None
    accepted_at: datetime | None
    decided_at: datetime | None
    created_at: datetime
    reward: RewardOut | None = None


class ReferralPage(BaseModel):
    items: list[ReferralOut]
    total: int


class CandidateReferralOut(BaseModel):
    """Candidate 360 "Referred by" badge."""

    referral_id: uuid.UUID
    human_id: str
    referrer_name: str | None
    referrer_kind: str
    status: ReferralStatus
    created_at: datetime


class LinkDemandIn(_In):
    demand_id: uuid.UUID


class VoidIn(_In):
    reason: str = Field(min_length=1, max_length=500)


class MarkPaidIn(_In):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    paid_on: date
    payout_ref: str = Field(min_length=1, max_length=100)


class MarkPaidOut(BaseModel):
    updated: int


# ================================================================ self-service inputs
class ShareLinkIn(_In):
    demand_id: uuid.UUID | None = None


class ReferIn(BaseModel):
    """Form fields of ``POST /me/referrals`` (multipart; the resume is the file part)."""

    demand_id: uuid.UUID | None = None
    referee_name: str = Field(min_length=1, max_length=200)
    referee_email: EmailStr
    referee_phone: str | None = Field(default=None, max_length=40)
    relationship: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)


# ================================================================ public referee page (purpose referral_link)
class PublicNotice(BaseModel):
    notice_id: uuid.UUID | None
    version: int | None
    title: str
    body_md: str


class PublicReferralView(BaseModel):
    mode: Literal["accept", "submit"]
    status: str
    tenant_name: str | None
    job_title: str | None
    general: bool
    referrer_first_name: str | None
    referee_first_name: str | None
    notice: PublicNotice
    expires_at: datetime


class PublicAcceptIn(_In):
    notice_id: uuid.UUID | None = None
    agreed: Literal[True]


class PublicReferralOut(BaseModel):
    status: str
