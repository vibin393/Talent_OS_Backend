# ruff: file-ignore[line-too-long]
"""DTOs for the Cross-tenant Job Network (Epic 75).

Public DTOs (``NetworkJob*``, ``NetworkAgency``) deliberately expose **no** ``tenant_id`` / ``member_id`` /
``posting_ref`` / hold reasons / report counts / stats -- ``tests/job_network/test_public_api.py`` scans every public
response for those keys.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ tenant admin -- settings & opt-in
class NetworkJoinIn(_Base):
    terms_version: str = Field(max_length=20)
    list_mode: str = Field(default="choose")
    show_logo: bool = True


class NetworkSettingsPatch(_Base):
    list_mode: str | None = None
    show_logo: bool | None = None
    exclude_client_ids: list[uuid.UUID] | None = None


class NetworkSettingsOut(_Base):
    joined: bool
    member_status: str | None = None
    verification_status: str | None = None
    list_mode: str
    show_logo: bool
    exclude_client_ids: list[uuid.UUID]
    terms_version_current: str
    terms_accepted_at: datetime | None = None
    listing_limit: int
    live_count: int
    suspended_reason: str | None = None
    sandbox: bool = False


class ListingStatusOut(_Base):
    listing_id: uuid.UUID | None = None
    status: str
    hold_reasons: list[str] = Field(default_factory=list)
    listed_at: datetime | None = None


class ListingEligibilityOut(_Base):
    eligible: bool
    reasons: list[str] = Field(default_factory=list)
    listing: ListingStatusOut | None = None


class MyListingsQuery(_Base):
    status: str | None = None
    q: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=50)


class MyListingOut(_Base):
    listing_id: uuid.UUID
    posting_ref: uuid.UUID
    title: str
    status: str
    hold_reasons: list[str] = Field(default_factory=list)
    views: int = 0
    apply_clicks: int = 0
    impressions: int = 0
    applications: int = 0
    listed_at: datetime | None = None


class MyListingsPage(_Base):
    items: list[MyListingOut]
    total: int
    page: int
    page_size: int


class SetChoiceIn(_Base):
    include: bool


class RequestReviewIn(_Base):
    note: str = Field(max_length=2000)


# ================================================================ public site
class NetworkJobSummary(_Base):
    id: uuid.UUID
    title: str
    agency: str
    agency_slug: str
    client_name: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    location_hidden: bool = False
    work_mode: str | None = None
    employment_type: str | None = None
    pay_min: Decimal | None = None
    pay_max: Decimal | None = None
    pay_currency: str | None = None
    pay_period: str | None = None
    published_at: datetime


class NetworkJobFacets(_Base):
    work_mode: dict[str, int] = Field(default_factory=dict)
    employment_type: dict[str, int] = Field(default_factory=dict)
    category: dict[str, int] = Field(default_factory=dict)
    cities: dict[str, int] = Field(default_factory=dict)


class NetworkJobPage(_Base):
    items: list[NetworkJobSummary]
    total: int
    page: int
    page_size: int
    facets: NetworkJobFacets | None = None


class NetworkJob(_Base):
    id: uuid.UUID
    title: str
    agency: str
    agency_slug: str
    agency_careers_url: str
    description_html: str
    client_name: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    location_hidden: bool = False
    work_mode: str | None = None
    employment_type: str | None = None
    experience_min: Decimal | None = None
    experience_max: Decimal | None = None
    skills: list[str] = Field(default_factory=list)
    category: str | None = None
    pay_min: Decimal | None = None
    pay_max: Decimal | None = None
    pay_currency: str | None = None
    pay_period: str | None = None
    apply_url: str
    posting_url: str
    published_at: datetime
    valid_through: datetime | None = None
    jsonld: dict = Field(default_factory=dict)


class NetworkAgency(_Base):
    display_name: str
    public_slug: str
    logo_url: str | None = None
    about_text: str | None = None
    careers_url: str
    open_jobs: int
    jobs: list[NetworkJobSummary] = Field(default_factory=list)


class NetworkSitemapItem(_Base):
    listing_id: uuid.UUID
    lastmod: datetime | None = None


class ReportIn(_Base):
    reason: str
    details: str | None = Field(default=None, max_length=1000)
    challenge_token: str | None = None


class ImpressionBeaconIn(_Base):
    listing_ids: list[uuid.UUID] = Field(max_length=50)


# ================================================================ analytics
class ListingAnalyticsRow(_Base):
    listing_id: uuid.UUID
    title: str
    impressions: int
    views: int
    apply_clicks: int
    applications: int
    conversion: float


class NetworkAnalyticsOut(_Base):
    rows: list[ListingAnalyticsRow]
    totals: ListingAnalyticsRow | None = None


# ================================================================ platform moderation
class ReasonIn(_Base):
    reason: str = Field(min_length=10, max_length=500)


class PlatformMemberOut(_Base):
    id: uuid.UUID
    display_name: str
    public_slug: str
    status: str
    verification_status: str
    violation_count: int
    suspended_reason: str | None = None
    requested_at: datetime | None = None


class PlatformListingOut(_Base):
    id: uuid.UUID
    title: str
    agency: str
    status: str
    hold_reasons: list[str] = Field(default_factory=list)
    open_reports: int = 0


class PlatformReportOut(_Base):
    id: uuid.UUID
    listing_id: uuid.UUID
    listing_title: str
    reason: str
    details: str | None = None
    status: str
    created_at: datetime


class OperatorRolesIn(_Base):
    roles: list[str]


class PlatformNetworkAnalyticsOut(_Base):
    by_status: dict[str, int] = Field(default_factory=dict)
    by_category: dict[str, int] = Field(default_factory=dict)
    by_country: dict[str, int] = Field(default_factory=dict)
