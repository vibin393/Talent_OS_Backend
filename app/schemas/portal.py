"""Candidate Portal and career-site admin schemas (Epic 9).

``Portal*`` models are the only shapes a signed-in candidate ever receives (``extra="forbid"`` allow-lists
built in ``app.services.portal.projection``). No notes, scorecards, scores, rates, margins, owners, internal
stage keys, reason codes or client names beyond those the candidate was shown.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

from app.schemas.careers import PublicApplyQuestion, PublicNotice
from app.schemas.pipeline import Question


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


Text200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


# ================================================================ me
class PortalCandidateRef(_Out):
    id: uuid.UUID
    first_name: str


class PortalBranding(_Out):
    logo_url: str | None = None
    brand_color: str | None = None


class PortalTenantRef(_Out):
    slug: str
    display_name: str
    branding: PortalBranding


class PortalMe(_Out):
    candidate: PortalCandidateRef
    tenant: PortalTenantRef


class PortalStatus(_Out):
    status: str


# ================================================================ profile
class PortalLocation(_Out):
    city: str | None = None
    state: str | None = None
    country: str | None = None


class PortalLocationIn(_In):
    city: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    state: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    country: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class PortalMoney(_Out):
    amount: str | None = None
    currency: str | None = None
    unit: str | None = None


class PortalMoneyIn(_In):
    amount: Annotated[str, StringConstraints(pattern=r"^\d{1,12}(\.\d{1,2})?$")] | None = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    unit: Literal["annual", "monthly", "daily", "hourly"] | None = None


class PortalProfile(_Out):
    full_name: str
    emails: list[str]
    phones: list[str]
    location: PortalLocation | None = None
    links: list[str]
    headline: str | None = None
    notice_period_days: int | None = None
    expected_compensation: PortalMoney | None = None
    willing_to_relocate: bool | None = None
    work_authorization: str | None = None


class PortalProfileUpdate(_In):
    full_name: Text200 | None = None
    phones: Annotated[list[Annotated[str, StringConstraints(max_length=40)]], Field(max_length=5)] | None = (
        None
    )
    location: PortalLocationIn | None = None
    links: Annotated[list[Annotated[str, StringConstraints(max_length=500)]], Field(max_length=10)] | None = (
        None
    )
    headline: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    notice_period_days: Annotated[int, Field(ge=0, le=365)] | None = None
    expected_compensation: PortalMoneyIn | None = None
    willing_to_relocate: bool | None = None
    work_authorization: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    # Present only to answer 422 ``email_change_via_request`` (emails change through a correction request).
    emails: Any = None


# ================================================================ resumes
class PortalResume(_Out):
    id: uuid.UUID
    version: int | None = None
    file_name: str
    size_bytes: int
    content_type: str
    uploaded_at: datetime
    download_url: str


# ================================================================ applications
class PortalTimelineEntry(_Out):
    label: str
    at: datetime


class PortalApplication(_Out):
    id: uuid.UUID
    title: str
    client_name: str | None = None
    stage_label: str
    status: Literal["active", "closed"]
    applied_at: datetime
    updated_at: datetime
    can_withdraw: bool


class PortalApplicationDetail(PortalApplication):
    timeline: list[PortalTimelineEntry]


class PortalWithdrawIn(_In):
    reason_code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None


class PortalWithdrawReason(_Out):
    code: str
    label: str


# ================================================================ interviews
class PortalInterview(_Out):
    id: uuid.UUID
    title: str
    round_name: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    timezone: str | None = None
    mode: str | None = None
    location: str | None = None
    join_url: str | None = None
    status: str


# ================================================================ offers (read model used by the export / dashboard)
class PortalOffer(_Out):
    id: uuid.UUID
    title: str | None = None
    status: str
    start_date: str | None = None
    compensation: PortalMoney | None = None
    expires_at: datetime | None = None


# ================================================================ AI screenings
class PortalScreening(_Out):
    id: uuid.UUID
    title: str
    status: str
    sent_at: datetime | None = None


class PortalScreeningDetail(PortalScreening):
    consent_required: bool
    notice: PublicNotice | None = None
    questions: list[PublicApplyQuestion]


class PortalScreeningConsentIn(_In):
    granted: bool
    notice_id: uuid.UUID | None = None


class PortalScreeningSubmitIn(_In):
    answers: dict[str, Any]


# ================================================================ privacy
class PortalConsent(_Out):
    id: uuid.UUID
    purpose: str
    client_name: str | None = None
    status: str
    granted_at: datetime | None = None
    withdrawn_at: datetime | None = None
    notice_version: int | None = None


class PortalWithdrawDetails(_Out):
    suggest_erasure: bool


class PortalConsentWithdrawOut(_Out):
    consent: PortalConsent
    details: PortalWithdrawDetails


class PortalConsentGrantIn(_In):
    purpose: Literal["ai_processing", "marketing"]
    notice_id: uuid.UUID


class PortalDataExport(_Out):
    id: uuid.UUID
    status: str
    requested_at: datetime
    completed_at: datetime | None = None
    expires_at: datetime | None = None
    download_url: str | None = None


class PortalPrivacyRequestIn(_In):
    kind: Literal["correction", "erasure"]
    details: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    fields: Annotated[list[Annotated[str, StringConstraints(max_length=60)]], Field(max_length=20)] | None = (
        None
    )


class PortalPrivacyRequest(_Out):
    id: uuid.UUID
    kind: str
    status: Literal["open", "in_progress", "completed", "rejected"]
    received_at: datetime
    due_at: datetime
    completed_at: datetime | None = None


# ================================================================ admin: career site
STAGE_LABEL_KEYS: frozenset[str] = frozenset(
    {
        "applied",
        "custom",
        "shortlisted",
        "submitted",
        "interviewing",
        "selected",
        "offered",
        "hired",
        "rejected",
        "withdrawn",
    }
)


class CareerSiteOut(BaseModel):
    slug: str
    enabled: bool
    display_name: str | None = None
    brand_color: str | None = None
    about_md: str | None = None
    about_html: str | None = None
    privacy_contact_email: str | None = None
    candidate_stage_labels: dict[str, str]
    default_stage_labels: dict[str, str]
    apply_requires_phone: bool
    logo_url: str | None = None
    header_url: str | None = None
    public_url: str
    updated_at: datetime | None = None


class CareerSiteUpdate(_In):
    enabled: bool | None = None
    display_name: Text200 | None = None
    brand_color: Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")] | None = None
    about_md: Annotated[str, StringConstraints(max_length=10000)] | None = None
    privacy_contact_email: EmailStr | None = None
    candidate_stage_labels: (
        dict[str, Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)]] | None
    ) = None
    apply_requires_phone: bool | None = None

    @field_validator("candidate_stage_labels")
    @classmethod
    def _keys(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is not None:
            unknown = sorted(set(value) - STAGE_LABEL_KEYS)
            if unknown:
                msg = f"unknown stage keys: {', '.join(unknown)}"
                raise ValueError(msg)
        return value


class PostingQuestionsIn(_In):
    apply_questions: Annotated[list[Question], Field(max_length=10)]


class PostingQuestionsOut(BaseModel):
    demand_id: uuid.UUID
    posting_id: uuid.UUID
    apply_questions: list[Question]
