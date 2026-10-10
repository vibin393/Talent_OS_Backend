# ruff: noqa: E501
"""Public API v1 resource shapes (Epic 67, spec A4). One class per resource; ``object`` is the resource name."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.public_v1.common import Money, PublicIn, PublicObject, open_enum


# ================================================================ candidates
class CandidateOut(PublicObject):
    full_name: str
    email: str | None = None
    phone: str | None = None
    location: dict[str, Any] | None = None
    current_title: str | None = None
    current_employer: str | None = None
    total_experience_months: int | None = None
    notice_period_days: int | None = None
    availability_date: date | None = None
    current_ctc: Money | None = None
    expected_ctc: Money | None = None
    source: str | None = Field(default=None, json_schema_extra=open_enum("manual", "public_api", "career_portal", "bulk_import", "referral", "job_board"))
    owner_id: uuid.UUID | None = None
    archived: bool = False
    possible_duplicate: bool | None = None


class ConsentIn(PublicIn):
    basis: Literal["notice_provided", "consent_captured"]
    notice_version: str | None = Field(default=None, max_length=40)
    captured_at: datetime
    channel: str = Field(min_length=1, max_length=40)


class CandidateCreateIn(PublicIn):
    full_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=40)
    current_title: str | None = Field(default=None, max_length=200)
    current_employer: str | None = Field(default=None, max_length=200)
    total_experience_months: int | None = Field(default=None, ge=0, le=1200)
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    availability_date: date | None = None
    source_detail: str | None = Field(default=None, max_length=500)
    owner_id: uuid.UUID | None = None
    consent: ConsentIn | None = None


class CandidateUpdateIn(PublicIn):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    current_title: str | None = Field(default=None, max_length=200)
    current_employer: str | None = Field(default=None, max_length=200)
    total_experience_months: int | None = Field(default=None, ge=0, le=1200)
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    availability_date: date | None = None


class ResumeUploadOut(PublicIn):
    resume_id: uuid.UUID
    candidate_id: uuid.UUID
    status: str


# ================================================================ applications
class ApplicationOut(PublicObject):
    candidate_id: uuid.UUID
    demand_id: uuid.UUID
    stage: str = Field(json_schema_extra=open_enum("applied", "shortlisted", "submitted", "interviewing", "selected", "offered", "hired", "rejected", "withdrawn"))
    stage_id: uuid.UUID
    stage_entered_at: datetime | None = None
    source: str | None = None
    owner_id: uuid.UUID | None = None


class ApplicationCreateIn(PublicIn):
    candidate_id: uuid.UUID
    demand_id: uuid.UUID
    source_detail: str | None = Field(default=None, max_length=200)


class StageMoveIn(PublicIn):
    to_stage: str = Field(min_length=1, max_length=60, description="A non-human-only stage key (applied, interviewing or a custom stage)", json_schema_extra=open_enum("applied", "interviewing"))
    note: str | None = Field(default=None, max_length=4000)


# ================================================================ demands / postings
class DemandOut(PublicObject):
    title: str
    client_id: uuid.UUID | None = None
    status: str = Field(json_schema_extra=open_enum("draft", "pending_approval", "open", "on_hold", "filled", "cancelled", "closed"))
    positions: int | None = None
    location: Any = None
    employment_type: str | None = None
    work_mode: str | None = None
    priority: str | None = None
    owner_id: uuid.UUID | None = None
    start_date: date | None = None
    target_closure_date: date | None = None
    bill_rate: Money | None = None


class DemandCreateIn(PublicIn):
    title: str = Field(min_length=1, max_length=200)
    client_id: uuid.UUID | None = None
    positions: int = Field(default=1, ge=1, le=1000)
    employment_type: str | None = Field(default=None, max_length=30)
    owner_id: uuid.UUID | None = None


class PostingOut(PublicObject):
    demand_id: uuid.UUID
    slug: str | None = None
    is_live: bool
    published_at: datetime | None = None
    unpublished_at: datetime | None = None


# ================================================================ engagement
class NoteOut(PublicObject):
    entity_type: str
    entity_id: uuid.UUID
    kind: str = Field(json_schema_extra=open_enum("note", "call", "meeting"))
    body: str
    occurred_at: datetime | None = None
    author_id: uuid.UUID | None = None


NoteEntity = Literal["candidate", "application", "demand", "client", "placement"]


class NoteCreateIn(PublicIn):
    entity_type: NoteEntity
    entity_id: uuid.UUID
    body: str = Field(min_length=1, max_length=10_000)


class ActivityCreateIn(PublicIn):
    kind: Literal["call", "meeting", "email_logged", "other"]
    entity_type: NoteEntity
    entity_id: uuid.UUID
    occurred_at: datetime
    summary: str = Field(min_length=1, max_length=2000)


class TaskOut(PublicObject):
    title: str
    notes: str | None = None
    assignee_id: uuid.UUID | None = None
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    due_at: datetime | None = None
    priority: str | None = None
    status: str = Field(json_schema_extra=open_enum("open", "done", "cancelled"))
    completed_at: datetime | None = None


class TaskCreateIn(PublicIn):
    title: str = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)
    assignee_id: uuid.UUID | None = None
    entity_type: str | None = Field(default=None, max_length=30)
    entity_id: uuid.UUID | None = None
    due_at: datetime | None = None
    priority: Literal["low", "normal", "high"] = "normal"


class TaskUpdateIn(PublicIn):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)
    assignee_id: uuid.UUID | None = None
    due_at: datetime | None = None
    priority: Literal["low", "normal", "high"] | None = None
    status: Literal["open", "done"] | None = None


# ================================================================ directory
class ClientOut(PublicObject):
    name: str
    industry: str | None = None
    website: str | None = None
    status: str | None = None
    owner_id: uuid.UUID | None = None


class ContactOut(PublicObject):
    client_id: uuid.UUID
    name: str
    title: str | None = None
    email: str | None = None
    phone: str | None = None


class UserOut(PublicObject):
    display_name: str
    email: str | None = None
    title: str | None = None
    team_ids: list[uuid.UUID] = Field(default_factory=list)
    business_unit_id: uuid.UUID | None = None
    active: bool


class PoolOut(PublicObject):
    name: str
    kind: str | None = None
    description: str | None = None
    member_count: int | None = None
    status: str | None = None


class PoolMemberOut(PublicIn):
    candidate_id: uuid.UUID
    added_at: datetime | None = None


class SkillOut(PublicObject):
    name: str
    slug: str
    category: str | None = None


# ================================================================ delivery
class SubmissionOut(PublicObject):
    application_id: uuid.UUID
    candidate_id: uuid.UUID
    demand_id: uuid.UUID
    client_id: uuid.UUID | None = None
    status: str
    sent_at: datetime | None = None
    bill_rate: Money | None = None
    pay_rate: Money | None = None


class InterviewOut(PublicObject):
    application_id: uuid.UUID
    candidate_id: uuid.UUID
    demand_id: uuid.UUID | None = None
    stage_name: str | None = None
    round: int | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    timezone: str | None = None
    mode: str | None = None
    status: str
    participant_user_ids: list[uuid.UUID] = Field(default_factory=list)


class OfferOut(PublicObject):
    application_id: uuid.UUID
    status: str = Field(json_schema_extra=open_enum("draft", "pending_approval", "approved", "released", "accepted", "declined", "withdrawn", "expired"))
    status_changed_at: datetime | None = None
    expires_at: datetime | None = None


class PlacementOut(PublicObject):
    candidate_id: uuid.UUID
    client_id: uuid.UUID | None = None
    legal_entity_id: uuid.UUID | None = None
    status: str
    role_title: str | None = None
    worker_type: str | None = None
    planned_start_date: date | None = None
    planned_end_date: date | None = None
    actual_start_date: date | None = None
    end_date: date | None = None


class WorkerOut(PublicObject):
    candidate_id: uuid.UUID
    worker_type: str | None = None
    manager_user_id: uuid.UUID | None = None
    pan: str | None = None


# ================================================================ finance
class TimesheetDay(PublicIn):
    date: date
    hours: str


class TimesheetOut(PublicObject):
    placement_id: uuid.UUID
    candidate_id: uuid.UUID | None = None
    period_start: date
    period_end: date
    status: str
    hours_by_day: list[TimesheetDay] = Field(default_factory=list)


class ExpenseClaimOut(PublicObject):
    placement_id: uuid.UUID | None = None
    candidate_id: uuid.UUID | None = None
    title: str | None = None
    status: str
    total_claimed: Money | None = None
    total_approved: Money | None = None
    categories: list[str] = Field(default_factory=list)


class InvoiceOut(PublicObject):
    number: str | None = None
    client_id: uuid.UUID | None = None
    legal_entity_id: uuid.UUID | None = None
    doc_date: date | None = None
    due_date: date | None = None
    status: str
    total: Money | None = None
    irn_status: str | None = None


class CreditNoteOut(InvoiceOut):
    original_invoice_id: uuid.UUID | None = None
    reason: str | None = None


class PaymentOut(PublicObject):
    client_id: uuid.UUID | None = None
    payment_date: date | None = None
    amount: Money | None = None
    mode: str | None = None
    reference: str | None = None
    allocation_status: str | None = None
    reversed: bool = False


# ================================================================ imports
class ImportJobOut(PublicIn):
    id: uuid.UUID
    object: str = "import"
    resource: str
    format: str
    status: str = Field(json_schema_extra=open_enum("queued", "validating", "running", "completed", "failed", "cancelled"))
    row_count: int
    batch_id: uuid.UUID | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    has_error_report: bool = False
    created_at: datetime | None = None
    completed_at: datetime | None = None
