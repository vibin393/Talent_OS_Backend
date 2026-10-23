"""Vendor Portal response / request models (Epic 17, FR-97 / FR-80). Every model is registered with
``register_portal_schema("vendor_user")`` so the Epic 14 forbidden-field CI test covers it.

Never here: bill rate, margin, client feedback, other vendors' anything, ``candidate_id``, internal notes,
match
scores; the client name only through ``ClientNameRevealed`` ("Confidential client" unless revealed).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.field_visibility import ClientNameRevealed, PrincipalKind, register_portal_schema
from app.core.money import DecimalStr

VENDOR: PrincipalKind = "vendor_user"


@register_portal_schema(VENDOR)
class PortalReleaseOut(BaseModel):
    id: uuid.UUID
    human_id: str
    status: str
    title: str
    positions: int
    skills: list[str]
    location: str | None
    employment_type: str | None
    experience_range: str | None
    jd_public_text: str | None
    client_name: ClientNameRevealed
    max_vendor_rate: DecimalStr | None
    rate_unit: str | None
    currency: str
    submission_limit: int | None
    submissions_used: int
    remaining_slots: int | None
    deadline_at: datetime | None
    instructions: str | None
    released_at: datetime


@register_portal_schema(VENDOR)
class PortalTimelineItem(BaseModel):
    label: str
    at: datetime


@register_portal_schema(VENDOR)
class PortalSubmissionOut(BaseModel):
    id: uuid.UUID
    human_id: str
    release_id: uuid.UUID
    release_title: str | None = None
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
    status: str
    label: str
    outcome_category: str | None
    next_interview_at: datetime | None
    placed: bool
    resume_file_id: uuid.UUID | None
    resume_locked: bool
    created_at: datetime
    timeline: list[PortalTimelineItem] = Field(default_factory=list)


@register_portal_schema(VENDOR)
class PortalPlacementOut(BaseModel):
    id: uuid.UUID
    human_id: str
    worker_name: str | None
    role_title: str | None
    start_date: date | None
    end_date: date | None
    status: str


@register_portal_schema(VENDOR)
class PortalDocumentOut(BaseModel):
    id: uuid.UUID
    category: str
    title: str
    expires_on: date | None
    status: str
    pending_review: bool
    file_id: uuid.UUID | None
    created_at: datetime


@register_portal_schema(VENDOR)
class PortalStatementOut(BaseModel):
    id: uuid.UUID
    human_id: str
    period_start: date
    period_end: date
    status: str
    currency: str
    total_amount: DecimalStr
    finalized_at: datetime | None


@register_portal_schema(VENDOR)
class PortalLabelCount(BaseModel):
    label: str
    count: int


@register_portal_schema(VENDOR)
class PortalUpcomingInterview(BaseModel):
    submission_id: uuid.UUID
    full_name: str | None
    at: datetime


@register_portal_schema(VENDOR)
class PortalDashboardOut(BaseModel):
    vendor_name: str
    vendor_status: str
    open_releases: int
    submissions_by_label: list[PortalLabelCount]
    upcoming_interviews: list[PortalUpcomingInterview]
    active_placements: int
    can_operate: bool


@register_portal_schema(VENDOR)
class PortalScorecardOut(BaseModel):
    period_from: date
    period_to: date
    releases_received: int
    submissions: int
    acceptance_pct: DecimalStr | None
    duplicate_pct: DecimalStr | None
    submission_to_interview_pct: DecimalStr | None
    interview_to_offer_pct: DecimalStr | None
    placements: int
    median_hours_to_first_submission: DecimalStr | None


@register_portal_schema(VENDOR)
class PortalSubmissionIn(BaseModel):
    """Multipart form fields of ``POST /vendor-portal/releases/{id}/submissions`` (validated server-side)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    full_name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    email: Annotated[str, StringConstraints(min_length=3, max_length=254)]
    phone_e164: str | None = Field(default=None, max_length=20)
    current_location: str | None = Field(default=None, max_length=200)
    total_experience_months: int | None = Field(default=None, ge=0, le=900)
    availability_date: date | None = None
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    vendor_rate: Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=4)]
    rate_unit: str | None = None
    currency: str | None = None
    attestation: bool = False
