# ruff: noqa: E501
"""Campus hiring & recruitment events (Epic 43): internal ``/api/v1/campus/*`` schemas. Public registration pages
use ``schemas/campus_public.py``. Decimals serialise as strings (AD-12)."""

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.core.money import DecimalStr

Tier = Literal["tier_1", "tier_2", "tier_3", "other"]
InstitutionStatus = Literal["active", "archived"]
DriveStatus = Literal["draft", "registration_open", "in_progress", "closed", "cancelled"]
RegistrationStatus = Literal[
    "registered",
    "eligible",
    "not_eligible",
    "assessed",
    "shortlisted",
    "interviewed",
    "offered",
    "joined",
    "reneged",
    "rejected",
    "withdrawn",
]
RegistrationSource = Literal["link", "import", "walk_in"]
FieldKind = Literal["text", "number", "decimal", "select", "multiselect", "date", "bool", "file"]
BatchStatus = Literal["draft", "scheduled", "completed", "cancelled"]
EventKind = Literal["job_fair", "walk_in", "hackathon", "virtual", "campus"]
EventStatus = Literal["draft", "open", "live", "closed", "cancelled"]
NotifyChannel = Literal["sms", "whatsapp", "none"]
CheckinStatus = Literal["registered", "checked_in", "no_show", "left"]
StationStatus = Literal["open", "paused", "closed"]
QueueStatus = Literal["waiting", "called", "in_interview", "done", "skipped", "left"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def _strip(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v or None


# ================================================================ settings
class CampusSettingsOut(_Out):
    register_ip_limit: int
    register_drive_limit: int
    avg_interview_minutes: int
    poster_footer: str | None


class CampusSettingsIn(_In):
    register_ip_limit: int = Field(ge=1, le=100)
    register_drive_limit: int = Field(ge=100, le=20000)
    avg_interview_minutes: int = Field(ge=5, le=180)
    poster_footer: str | None = Field(default=None, max_length=300)


# ================================================================ institutions / placement officers
class Program(_In):
    name: str = Field(min_length=1, max_length=200)
    branches: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("branches")
    @classmethod
    def _branches(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for b in v:
            b = (b or "").strip()
            if not b or len(b) > 200:
                msg = "Branch names are 1 to 200 characters"
                raise ValueError(msg)
            if b not in out:
                out.append(b)
        return out


class InstitutionIn(_In):
    name: str = Field(min_length=2, max_length=300)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    country: str = Field(default="IN", min_length=2, max_length=2)
    tier: Tier = "other"
    programs: list[Program] = Field(default_factory=list, max_length=100)
    website: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=5000)

    @field_validator("country")
    @classmethod
    def _country(cls, v: str) -> str:
        return v.upper()


class InstitutionPatch(_In):
    name: str | None = Field(default=None, min_length=2, max_length=300)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    tier: Tier | None = None
    programs: list[Program] | None = Field(default=None, max_length=100)
    website: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=5000)
    status: InstitutionStatus | None = None


class ContactIn(_In):
    name: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    is_primary: bool = False


class ContactPatch(_In):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    is_primary: bool | None = None


class ContactOut(_Out):
    id: uuid.UUID
    institution_id: uuid.UUID
    name: str
    title: str | None
    email: str | None
    phone: str | None
    is_primary: bool
    archived_at: datetime | None


class InstitutionOut(_Out):
    id: uuid.UUID
    name: str
    city: str | None
    state: str | None
    country: str
    tier: Tier
    programs: list[dict[str, Any]]
    website: str | None
    status: InstitutionStatus
    notes: str | None
    contact_count: int = 0
    primary_contact_name: str | None = None
    created_at: datetime
    updated_at: datetime


class InstitutionDetailOut(InstitutionOut):
    contacts: list[ContactOut] = []


class InstitutionImportIn(_In):
    """Go-live import row (FR-263; CSV column names = field names). ``programs`` is ``Program:Branch1|Branch2;...``;
    an optional placement officer goes in ``contact_*``."""

    name: str = Field(min_length=2, max_length=300)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    country: str = Field(default="IN", min_length=2, max_length=2)
    tier: Tier = "other"
    programs: str | None = Field(default=None, max_length=5000)
    website: str | None = Field(default=None, max_length=500)
    contact_name: str | None = Field(default=None, max_length=200)
    contact_title: str | None = Field(default=None, max_length=200)
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=40)


# ================================================================ forms
class FormField(_In):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    label: str = Field(min_length=1, max_length=200)
    type: FieldKind
    required: bool = False
    options: list[str] | None = Field(default=None, max_length=200)
    min: Decimal | None = None
    max: Decimal | None = None
    help: str | None = Field(default=None, max_length=500)
    standard: bool = False


class FormSchema(_In):
    fields: list[FormField] = Field(default_factory=list, max_length=40)


# ================================================================ drives
class DriveIn(_In):
    name: str = Field(min_length=2, max_length=200)
    batch_year: int = Field(ge=2000, le=2100)
    owner_id: uuid.UUID | None = None
    registration_opens_at: datetime | None = None
    registration_closes_at: datetime | None = None
    assessment_window: dict[str, Any] | None = None
    interview_window: dict[str, Any] | None = None
    form_schema: FormSchema | None = None
    assessment_id: uuid.UUID | None = None
    interview_stage_template_id: uuid.UUID | None = None
    offer_template_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=5000)


class DrivePatch(_In):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    batch_year: int | None = Field(default=None, ge=2000, le=2100)
    owner_id: uuid.UUID | None = None
    registration_opens_at: datetime | None = None
    registration_closes_at: datetime | None = None
    assessment_window: dict[str, Any] | None = None
    interview_window: dict[str, Any] | None = None
    form_schema: FormSchema | None = None
    assessment_id: uuid.UUID | None = None
    interview_stage_template_id: uuid.UUID | None = None
    offer_template_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=5000)


class DriveInstitutionIn(_In):
    institution_id: uuid.UUID
    registration_cap: int | None = Field(default=None, ge=1, le=100000)


class DriveInstitutionPatch(_In):
    registration_cap: int | None = Field(default=None, ge=1, le=100000)
    status: Literal["active", "paused"] | None = None


class DriveInstitutionOut(BaseModel):
    id: uuid.UUID
    institution_id: uuid.UUID
    institution_name: str
    status: Literal["active", "paused"]
    registration_cap: int | None
    registration_url: str | None
    link_expires_at: datetime | None
    link_active: bool
    has_qr: bool
    registration_count: int


class DriveDemandIn(_In):
    demand_id: uuid.UUID


class DemandRef(BaseModel):
    id: uuid.UUID
    title: str
    req_id: str | None = None
    status: str | None = None


class ReasonIn(_In):
    reason: str = Field(min_length=5, max_length=500)


class DriveOut(BaseModel):
    id: uuid.UUID
    name: str
    batch_year: int
    status: DriveStatus
    owner_id: uuid.UUID
    owner_name: str | None
    registration_opens_at: datetime | None
    registration_closes_at: datetime | None
    assessment_window: dict[str, Any] | None
    interview_window: dict[str, Any] | None
    form_schema: dict[str, Any]
    eligibility_definition: dict[str, Any] | None
    eligibility_revision: int
    assessment_id: uuid.UUID | None
    assessment_name: str | None = None
    interview_stage_template_id: uuid.UUID | None
    offer_template_id: uuid.UUID | None
    notes: str | None
    cancel_reason: str | None
    institutions: list[DriveInstitutionOut]
    demands: list[DemandRef]
    status_counts: dict[str, int]
    created_at: datetime
    updated_at: datetime


class DriveListItem(BaseModel):
    id: uuid.UUID
    name: str
    batch_year: int
    status: DriveStatus
    owner_id: uuid.UUID
    owner_name: str | None
    registration_closes_at: datetime | None
    institution_count: int
    registration_count: int
    created_at: datetime


class EligibilityIn(_In):
    """The AD-42 filter tree over ``campus.registration`` (``null`` clears the rule: everybody is eligible)."""

    filters: dict[str, Any] | None = None


# ================================================================ registrations
class FailingRule(BaseModel):
    index: int
    field: str | None
    op: str | None
    value: Any = None
    label: str


class EligibilityOut(BaseModel):
    eligible: bool
    failing_rule: FailingRule | None = None
    evaluated_at: datetime | None = None
    definition_version: int | None = None
    revision: int | None = None


class RegistrationOut(BaseModel):
    id: uuid.UUID
    drive_id: uuid.UUID
    institution_id: uuid.UUID
    institution_name: str | None
    candidate_id: uuid.UUID
    candidate_name: str | None
    candidate_email: str | None
    status: RegistrationStatus
    source: RegistrationSource
    program: str | None
    branch: str | None
    cgpa: DecimalStr | None
    percentage: DecimalStr | None
    backlogs: int | None
    graduation_year: int | None
    answers: dict[str, Any]
    eligibility: EligibilityOut | None
    overridden: bool
    override_reason: str | None
    override_at: datetime | None
    assessment_invitation_id: uuid.UUID | None
    assessment_status: str | None = None
    assessment_flag: str | None
    score: DecimalStr | None
    application_id: uuid.UUID | None
    offer_id: uuid.UUID | None
    offer_status: str | None = None
    planned_joining_date: date | None
    registered_at: datetime
    eligible_at: datetime | None
    assessed_at: datetime | None
    shortlisted_at: datetime | None
    interviewed_at: datetime | None
    offered_at: datetime | None
    accepted_at: datetime | None
    joined_at: datetime | None
    reneged_at: datetime | None
    rejected_at: datetime | None
    withdrawn_at: datetime | None


class RegistrationPage(BaseModel):
    items: list[RegistrationOut]
    total: int
    limit: int
    offset: int


class OverrideIn(_In):
    reason: str = Field(min_length=5, max_length=1000)


class NoteIn(_In):
    note: str | None = Field(default=None, max_length=1000)


class RenegeIn(_In):
    note: str = Field(min_length=2, max_length=900)


# ================================================================ import
class ImportRowOut(BaseModel):
    row: int
    email: str | None
    name: str | None
    errors: list[str]


class ImportPreviewOut(BaseModel):
    batch_id: uuid.UUID
    drive_institution_id: uuid.UUID
    row_count: int
    error_count: int
    rows: list[ImportRowOut]


class ImportApplyOut(BaseModel):
    batch_id: uuid.UUID
    sent: int
    skipped: int


# ================================================================ assessments / shortlist
class InviteIn(_In):
    registration_ids: list[uuid.UUID] | None = Field(default=None, max_length=20000)
    deadline_at: datetime


class ItemError(BaseModel):
    id: uuid.UUID
    code: str
    message: str | None = None


class InviteOut(BaseModel):
    invited: int
    skipped: int
    batches: int
    errors: list[ItemError]


class ProposeIn(_In):
    mode: Literal["top_n", "min_score"]
    value: DecimalStr = Field(ge=0)
    per_demand: bool = False


class ProposalItem(BaseModel):
    registration_id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_name: str | None
    institution_name: str | None
    score: DecimalStr | None
    tie: bool = False


class ProposalOut(BaseModel):
    mode: str
    value: DecimalStr
    cutoff: DecimalStr | None
    count: int
    ties: int
    items: list[ProposalItem]


class ConfirmIn(_In):
    registration_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)
    demand_id: uuid.UUID


class ConfirmResult(BaseModel):
    registration_id: uuid.UUID
    application_id: uuid.UUID | None = None
    error: str | None = None


class ConfirmOut(BaseModel):
    shortlisted: int
    results: list[ConfirmResult]


# ================================================================ interview batches
class Panel(_In):
    label: str = Field(min_length=1, max_length=100)
    interviewer_user_ids: list[uuid.UUID] = Field(min_length=1, max_length=10)
    room: str | None = Field(default=None, max_length=300)
    video_link: str | None = Field(default=None, max_length=2000)

    @field_validator("video_link")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v and not v.lower().startswith(("https://", "http://")):
            msg = "video_link must be an http(s) URL"
            raise ValueError(msg)
        return v or None


class BatchIn(_In):
    demand_id: uuid.UUID
    date: date
    start_time: time
    end_time: time
    timezone: str = Field(default="Asia/Kolkata", max_length=64)
    slot_minutes: int = Field(default=30, ge=15, le=180)
    panels: list[Panel] = Field(min_length=1, max_length=30)
    send_sms: bool = False

    @model_validator(mode="after")
    def _times(self) -> "BatchIn":
        if self.end_time <= self.start_time:
            msg = "end_time must be after start_time"
            raise ValueError(msg)
        return self


class SlotOut(BaseModel):
    id: uuid.UUID
    panel_index: int
    starts_at: datetime
    ends_at: datetime
    registration_id: uuid.UUID | None
    candidate_name: str | None
    interview_id: uuid.UUID | None


class BatchOut(BaseModel):
    id: uuid.UUID
    drive_id: uuid.UUID
    demand_id: uuid.UUID
    demand_title: str | None
    date: date
    start_time: time
    end_time: time
    timezone: str
    slot_minutes: int
    panels: list[dict[str, Any]]
    status: BatchStatus
    send_sms: bool
    slot_count: int
    filled_count: int
    slots: list[SlotOut] = []


class AutoSlotOut(BaseModel):
    batch: BatchOut
    slotted: int
    unslotted: int
    errors: list[ItemError]


class MoveSlotIn(_In):
    to_slot_id: uuid.UUID


# ================================================================ bulk offers / funnel
class OfferOverrides(_In):
    pay_rate: DecimalStr | None = Field(default=None, gt=Decimal(0), max_digits=14, decimal_places=2)
    pay_currency: str = Field(default="INR", min_length=3, max_length=3)
    pay_rate_unit: Literal["hour", "day", "month", "year"] | None = None
    employment_type: Literal["contract", "contract_to_hire", "permanent", "freelance"] | None = None
    expiry_date: date | None = None
    location: str | None = Field(default=None, max_length=200)


class BulkOffersIn(_In):
    registration_ids: list[uuid.UUID] = Field(min_length=1, max_length=2000)
    planned_joining_date: date
    overrides: OfferOverrides | None = None


class BulkOfferItemOut(BaseModel):
    registration_id: uuid.UUID
    offer_id: uuid.UUID | None = None
    offer_status: str | None = None
    error_code: str | None = None
    message: str | None = None


class BulkOffersOut(BaseModel):
    created: int
    failed: int
    results: list[BulkOfferItemOut]


class JoiningRow(BaseModel):
    registration_id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_name: str | None
    institution_name: str | None
    status: RegistrationStatus
    offer_id: uuid.UUID | None
    offer_status: str | None
    planned_joining_date: date | None
    placement_id: uuid.UUID | None = None
    placement_status: str | None = None
    accepted_at: datetime | None
    joined_at: datetime | None
    reneged_at: datetime | None


class FunnelRow(BaseModel):
    institution_id: uuid.UUID
    institution_name: str
    registered: int = 0
    eligible: int = 0
    assessed: int = 0
    shortlisted: int = 0
    interviewed: int = 0
    offered: int = 0
    accepted: int = 0
    joined: int = 0
    reneged: int = 0


class FunnelOut(BaseModel):
    drive_id: uuid.UUID | None
    stages: list[str]
    rows: list[FunnelRow]
    totals: FunnelRow


# ================================================================ recruitment events
class EventIn(_In):
    name: str = Field(min_length=2, max_length=200)
    kind: EventKind
    drive_id: uuid.UUID | None = None
    demand_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    starts_at: datetime
    ends_at: datetime
    timezone: str = Field(default="Asia/Kolkata", max_length=64)
    venue: str | None = Field(default=None, max_length=500)
    video_link: str | None = Field(default=None, max_length=2000)
    avg_interview_minutes: int | None = Field(default=None, ge=5, le=180)
    notify_channel: NotifyChannel = "none"

    @model_validator(mode="after")
    def _times(self) -> "EventIn":
        if self.ends_at <= self.starts_at:
            msg = "ends_at must be after starts_at"
            raise ValueError(msg)
        return self


class EventPatch(_In):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    demand_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    venue: str | None = Field(default=None, max_length=500)
    video_link: str | None = Field(default=None, max_length=2000)
    avg_interview_minutes: int | None = Field(default=None, ge=5, le=180)
    notify_channel: NotifyChannel | None = None


class EventCounts(BaseModel):
    registered: int = 0
    checked_in: int = 0
    waiting: int = 0
    in_interview: int = 0
    interviewed: int = 0


class EventOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: EventKind
    drive_id: uuid.UUID | None
    owner_id: uuid.UUID
    demands: list[DemandRef]
    starts_at: datetime
    ends_at: datetime
    timezone: str
    venue: str | None
    video_link: str | None
    status: EventStatus
    registration_url: str | None
    avg_interview_minutes: int
    notify_channel: NotifyChannel
    station_count: int
    counts: EventCounts
    cancel_reason: str | None
    created_at: datetime


class StationIn(_In):
    label: str = Field(min_length=1, max_length=100)
    interviewer_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    demand_id: uuid.UUID | None = None


class StationPatch(_In):
    label: str | None = Field(default=None, min_length=1, max_length=100)
    interviewer_user_ids: list[uuid.UUID] | None = Field(default=None, max_length=10)
    demand_id: uuid.UUID | None = None
    status: StationStatus | None = None


class StationOut(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    label: str
    interviewer_user_ids: list[uuid.UUID]
    demand_id: uuid.UUID | None
    status: StationStatus
    current_entry_id: uuid.UUID | None = None


class WalkInIn(_In):
    full_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=40)


class CheckInIn(_In):
    """Exactly one of ``token`` (QR scan), ``candidate_id`` (search) or ``walk_in`` (form)."""

    token: str | None = Field(default=None, min_length=20, max_length=2000)
    candidate_id: uuid.UUID | None = None
    walk_in: WalkInIn | None = None
    demand_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one(self) -> "CheckInIn":
        if sum(x is not None for x in (self.token, self.candidate_id, self.walk_in)) != 1:
            msg = "Give exactly one of token, candidate_id or walk_in"
            raise ValueError(msg)
        return self


class CheckinOut(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_name: str | None
    demand_id: uuid.UUID | None
    demand_title: str | None
    status: CheckinStatus
    method: str
    registered_at: datetime
    checked_in_at: datetime | None
    queue_entry_id: uuid.UUID | None = None
    queue_position: int | None = None
    queue_status: QueueStatus | None = None


class QueueEntryOut(BaseModel):
    id: uuid.UUID
    position: int
    candidate_id: uuid.UUID
    candidate_name: str | None
    demand_id: uuid.UUID | None
    demand_title: str | None
    status: QueueStatus
    station_id: uuid.UUID | None
    station_label: str | None
    application_id: uuid.UUID | None
    interview_id: uuid.UUID | None
    ahead: int | None = None
    eta_minutes: int | None = None
    called_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    notified_at: datetime | None
    notify_result: str | None


class QueueOut(BaseModel):
    event_id: uuid.UUID
    status: EventStatus
    open_stations: int
    median_minutes: int
    waiting: int
    entries: list[QueueEntryOut]
    stations: list[StationOut]
    generated_at: datetime


class EventDashboardOut(BaseModel):
    event_id: uuid.UUID
    registered: int
    checked_in: int
    waiting: int
    called: int
    in_interview: int
    interviewed: int
    skipped: int
    left: int
    average_wait_minutes: int | None
    outcomes: dict[str, int]


class EventCheckinsOut(BaseModel):
    items: list[CheckinOut]
    total: int
