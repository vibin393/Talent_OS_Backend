"""Offer request / response models (Epic 8). Money follows AD-12 (``{"amount": "1800.00", "currency": "INR"}``).

Bill rate, burden and margin are ``None`` in every response for principals without ``compensation:view``
(``compensation_masked=true``, AD-11); candidate-facing models (``CandidateOfferView``) never carry them.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.approvals import RequestOut

EmploymentType = Literal["contract", "contract_to_hire", "permanent", "freelance"]
WorkMode = Literal["onsite", "hybrid", "remote"]
RateUnit = Literal["hour", "day", "month", "year"]
OfferStatus = Literal[
    "draft",
    "pending_approval",
    "approved",
    "released",
    "viewed",
    "accepted",
    "declined",
    "expired",
    "withdrawn",
]
ApprovalState = Literal["not_required", "pending", "approved", "returned"]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Money(BaseModel):
    amount: str
    currency: str


class MoneyIn(_In):
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    currency: Currency


# ================================================================ settings
class OfferSettingsOut(BaseModel):
    default_expiry_days: int
    signatory_name: str | None
    signatory_title: str | None
    response_link_grace_hours: int
    attach_letter_to_release_email: bool


class OfferSettingsUpdate(_In):
    default_expiry_days: int | None = Field(default=None, ge=1, le=90)
    signatory_name: str | None = Field(default=None, max_length=200)
    signatory_title: str | None = Field(default=None, max_length=200)
    response_link_grace_hours: int | None = Field(default=None, ge=0, le=168)
    attach_letter_to_release_email: bool | None = None


# ================================================================ templates
class MergeFieldOut(BaseModel):
    key: str
    label: str
    example: str


class OfferTemplateIn(_In):
    name: str = Field(min_length=1, max_length=200)
    employment_types: list[EmploymentType] = Field(default_factory=list)
    body_md: str = Field(min_length=1, max_length=50000)
    is_default: bool = False


class OfferTemplateUpdate(_In):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    employment_types: list[EmploymentType] | None = None
    body_md: str | None = Field(default=None, min_length=1, max_length=50000)
    is_default: bool | None = None


class OfferTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    employment_types: list[str]
    body_md: str
    is_default: bool
    version: int
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TemplatePreviewIn(_In):
    body_md: str = Field(min_length=1, max_length=50000)


# ================================================================ offers
class OfferTermsIn(_In):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    employment_type: EmploymentType | None = None
    pay_rate: MoneyIn | None = None
    pay_rate_unit: RateUnit | None = None
    bill_rate: MoneyIn | None = None
    bill_rate_unit: RateUnit | None = None
    bonus: MoneyIn | None = None
    benefits_summary: str | None = Field(default=None, max_length=5000)
    start_date: date | None = None
    end_date: date | None = None
    location: str | None = Field(default=None, max_length=300)
    work_mode: WorkMode | None = None
    expiry_date: date | None = None
    template_id: uuid.UUID | None = None


class OfferCreate(OfferTermsIn):
    """``POST /applications/{id}/offers``: every field is optional (prefilled from the demand and the latest
    sent submission)."""


class OfferUpdate(OfferTermsIn):
    lock_version: int = Field(ge=1)


class WithdrawIn(_In):
    reason: str = Field(min_length=5, max_length=2000)


class OfferVersionBrief(BaseModel):
    version_no: int
    approval_state: ApprovalState
    frozen: bool
    created_at: datetime
    released: bool = False


class OfferVersionOut(BaseModel):
    version_no: int
    title: str
    employment_type: EmploymentType
    pay_rate: Money
    pay_rate_unit: RateUnit
    bill_rate: Money | None
    bill_rate_unit: RateUnit | None
    burden_pct: str | None
    margin_amount: Money | None
    margin_pct: str | None
    min_margin_pct: str | None = None
    below_min_margin: bool | None = None
    bonus: Money | None
    benefits_summary: str | None
    start_date: date
    end_date: date | None
    location: str | None
    work_mode: WorkMode | None
    expiry_date: date
    template_id: uuid.UUID | None
    template_version: int | None
    letter_file_id: uuid.UUID | None
    letter_sha256: str | None
    letter_ready: bool
    approval_state: ApprovalState
    approved_at: datetime | None
    frozen: bool
    created_by: uuid.UUID | None
    created_at: datetime


class OfferStatusHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_status: str | None
    to_status: str
    actor_kind: str
    actor_id: uuid.UUID | None
    version_no: int | None
    reason: str | None
    at: datetime


class OfferResponseOut(BaseModel):
    decision: Literal["accepted", "declined"]
    version_no: int
    typed_name: str | None
    decline_reason: str | None
    ip: str | None
    user_agent: str | None
    channel: Literal["portal", "secure_link"]
    letter_sha256: str | None
    decided_at: datetime


class OfferOut(BaseModel):
    id: uuid.UUID
    human_id: str
    status: OfferStatus
    application_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    client_id: uuid.UUID
    candidate_name: str | None = None
    demand_title: str | None = None
    client_name: str | None = None
    owner_id: uuid.UUID
    lock_version: int
    current_version: OfferVersionOut
    released_version_no: int | None
    approval_request_id: uuid.UUID | None
    status_comment: str | None
    withdrawn_reason: str | None
    released_at: datetime | None
    viewed_at: datetime | None
    responded_at: datetime | None
    expires_at: datetime | None
    accepted_letter_url: str | None = None
    compensation_masked: bool
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class OfferDetailOut(OfferOut):
    versions: list[OfferVersionBrief]
    history: list[OfferStatusHistoryOut]
    approvals: list[RequestOut]
    responses: list[OfferResponseOut]


# ================================================================ candidate-facing
class CandidateOfferResponse(BaseModel):
    decision: Literal["accepted", "declined"]
    version_no: int
    typed_name: str | None
    decided_at: datetime


class CandidateOfferView(BaseModel):
    """What a candidate sees (portal and secure link). Never bill rate, burden or margin."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    reference: str
    status: OfferStatus
    version_no: int
    tenant_name: str | None
    candidate_name: str | None
    title: str
    employment_type: EmploymentType
    pay_rate: Money
    pay_rate_unit: RateUnit
    bonus: Money | None
    benefits_summary: str | None
    start_date: date
    end_date: date | None
    location: str | None
    work_mode: WorkMode | None
    expiry_date: date
    expires_at: datetime | None
    released_at: datetime | None
    letter_available: bool
    letter_url: str | None
    can_respond: bool
    response: CandidateOfferResponse | None


class AcceptIn(_In):
    typed_name: str = Field(min_length=1, max_length=300)
    version_no: int = Field(ge=1)
    agreed: bool
    # Epic 22: the emailed one-time code when the tenant requires it (offer_settings.acceptance_requires_otp).
    otp: str | None = Field(default=None, max_length=12)


class DeclineIn(_In):
    reason: str | None = Field(default=None, max_length=2000)
    version_no: int = Field(ge=1)


class OfferActionOut(BaseModel):
    status: str
    offer: CandidateOfferView | None = None


class CandidateOfferList(BaseModel):
    items: list[CandidateOfferView]
