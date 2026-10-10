"""Public Career Portal schemas (Epic 9, ``/api/v1/public/careers/{slug}``).

Every ``Public*`` response model is an explicit allow-list (``extra="forbid"``) built only in
``app.services.portal.projection``: bill rates, margins, owners, req ids, client ids, hidden client names and
internal stage keys have no field here to leak through.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ branding
class PublicBranding(_Out):
    slug: str
    display_name: str
    logo_url: str | None = None
    header_url: str | None = None
    brand_color: str | None = None
    about_html: str | None = None
    privacy_contact_email: str | None = None
    sign_in_url: str


# ================================================================ postings
class PublicExperience(_Out):
    min: float | None = None
    max: float | None = None


class PublicPayRange(_Out):
    min: str | None = None
    max: str | None = None
    currency: str | None = None
    unit: str | None = None


class PublicPostingSummary(_Out):
    id: uuid.UUID
    slug: str
    title: str
    location: str | None = None
    country: str | None = None
    work_mode: str | None = None
    employment_type: str | None = None
    client_name: str | None = None
    published_at: datetime | None = None


class PublicPostingPage(_Out):
    items: list[PublicPostingSummary]
    total: int
    page: int
    page_size: int


class PublicPosting(_Out):
    id: uuid.UUID
    slug: str
    title: str
    description_html: str
    work_mode: str | None = None
    employment_type: str | None = None
    experience: PublicExperience
    skills: list[str]
    published_at: datetime | None = None
    location: str | None = None
    country: str | None = None
    client_name: str | None = None
    pay_range: PublicPayRange | None = None
    jsonld: dict[str, Any]


class PublicSitemapEntry(_Out):
    posting_id: uuid.UUID
    slug: str
    lastmod: datetime | None = None


# ================================================================ apply
class PublicNotice(_Out):
    id: uuid.UUID
    purpose: str
    version: int
    title: str
    body: str
    body_html: str


class PublicApplyQuestion(_Out):
    key: str
    label: str
    type: str
    options: list[str] | None = None
    required: bool = False


class PublicChallenge(_Out):
    provider: str
    site_key: str


class PublicApplyForm(_Out):
    posting_id: uuid.UUID
    title: str
    notices: list[PublicNotice]
    apply_questions: list[PublicApplyQuestion]
    max_upload_mb: int
    accepted_types: list[str]
    challenge: PublicChallenge
    requires_phone: bool


ApplyPurpose = Literal["data_processing", "ai_processing"]


class ApplyConsentIn(_In):
    purpose: ApplyPurpose
    notice_id: uuid.UUID


class ApplySessionCreate(_In):
    consents: Annotated[list[ApplyConsentIn], Field(min_length=1, max_length=2)]
    challenge_token: Annotated[str, StringConstraints(max_length=4096)] | None = None
    # Epic 70 (D-18): careers attribution from the apply link (``?src=naukri&bp=<id>``); unknown tags are ignored.
    src: Annotated[str, StringConstraints(max_length=40, pattern=r"^[a-z_]+$")] | None = None
    ref: Annotated[str, StringConstraints(max_length=64)] | None = None


class PublicApplySession(_Out):
    apply_token: str
    expires_at: datetime


class PublicPrefill(_Out):
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    current_title: str | None = None
    current_employer: str | None = None
    total_experience_months: int | None = None
    skills: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)


class PublicResumeResult(_Out):
    parse_status: str
    prefill: PublicPrefill


class ApplySubmit(_In):
    full_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    email: EmailStr
    phone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None
    location: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    links: Annotated[list[Annotated[str, StringConstraints(max_length=500)]], Field(max_length=10)] = Field(
        default_factory=list
    )
    answers: dict[str, Any] = Field(default_factory=dict)
    marketing_consent_notice_id: uuid.UUID | None = None
    # Epic 39: the ``tos_ct`` marketing link token -> application ``source='campaign'`` when valid (else ignored).
    attribution_token: Annotated[str, StringConstraints(max_length=120)] | None = None


class PublicStatus(_Out):
    status: str


# ================================================================ candidate sign-in
class CandidateLinkRequest(_In):
    tenant_slug: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    email: EmailStr
    challenge_token: Annotated[str, StringConstraints(max_length=4096)] | None = None


class CandidateLinkVerify(_In):
    token: Annotated[str, StringConstraints(min_length=20, max_length=200)]
