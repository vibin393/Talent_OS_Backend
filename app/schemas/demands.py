"""Demand API schemas (Epic 4). Compensation fields (``bill_rate``, ``pay_rate_min/max``,
``margin_percent``) are masked by ``core.permissions.mask_compensation`` for callers without
``compensation:view`` (``masked_fields`` lists them)."""

import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

DemandStatus = Literal["draft", "pending_approval", "open", "on_hold", "filled", "cancelled", "closed"]
EmploymentType = Literal["contract", "contract_to_hire", "permanent", "freelance"]
WorkMode = Literal["onsite", "hybrid", "remote"]
Priority = Literal["low", "medium", "high", "critical"]
RateUnit = Literal["hour", "day", "month", "year"]
DemandVisibility = Literal["external", "internal", "both"]  # Epic 36 (AD-50): careers visibility
SkillKind = Literal["mandatory", "preferred"]
SkillRefKind = Literal["global", "tenant"]
Progress = Literal["recruiting", "submitted", "interviewing", "offer", "partially_filled"]
JdKind = Literal[
    "full", "short", "posting", "recruiter_summary", "screening_questions", "interview_questions", "boolean"
]
SlaMilestone = Literal["assignment", "first_submission", "first_interview", "client_feedback"]
SlaStatus = Literal["healthy", "at_risk", "breached", "met"]
SlaClockType = Literal["business", "calendar"]
DemandSort = Literal[
    "created_at", "-created_at", "target_closure_date", "-target_closure_date", "priority", "-priority"
]

Text200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text300 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


def _upper(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


Country = Annotated[str, BeforeValidator(_upper), StringConstraints(pattern=r"^[A-Z]{2}$")]
Currency = Annotated[str, BeforeValidator(_upper), StringConstraints(pattern=r"^[A-Z]{3}$")]
Money = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]
Years = Annotated[Decimal, Field(ge=0, le=99, max_digits=4, decimal_places=1)]
ShortList = Annotated[list[Text200], Field(max_length=50)]

# Fields a client may never write (422 when sent).
READ_ONLY_FIELDS = frozenset(
    {
        "req_id",
        "status",
        "progress",
        "applications_count",
        "submissions_count",
        "interviews_scheduled_count",
        "offers_released_count",
        "positions_filled",
        "opened_at",
        "archived_at",
        "ai_extracted_fields",
        "published_jd_version_id",
        "intake_ai_action_id",
        "status_reason_code",
        "status_comment",
    }
)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemandSkillIn(_In):
    label: Text200
    skill_id: uuid.UUID | None = None
    skill_ref_kind: SkillRefKind | None = None
    kind: SkillKind = "mandatory"
    min_years: Years | None = None
    # Epic 37 (FR-190): minimum proficiency (1-5) and whether related skills (graph expansion) count.
    min_proficiency: Annotated[int, Field(ge=1, le=5)] | None = None
    expand: bool = True


class InterviewPlanStep(_In):
    name: Text200
    kind: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)] = "interview"


class _DemandFields(_In):
    @model_validator(mode="before")
    @classmethod
    def _no_read_only(cls, data: Any) -> Any:
        if isinstance(data, dict):
            sent = sorted(READ_ONLY_FIELDS & set(data))
            if sent:
                raise ValueError(f"read-only fields cannot be set: {', '.join(sent)}")
        return data

    end_client: Text300 | None = None
    hiring_manager_contact_id: uuid.UUID | None = None
    department: Text200 | None = None
    employment_type: EmploymentType | None = None
    country: Country | None = None
    location: Text300 | None = None
    work_mode: WorkMode | None = None
    min_experience_years: Years | None = None
    max_experience_years: Years | None = None
    education: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    certifications: ShortList | None = None
    work_authorization: ShortList | None = None
    clearance: Text200 | None = None
    languages: ShortList | None = None
    shift: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    travel_percent: Annotated[int, Field(ge=0, le=100)] | None = None
    start_date: date | None = None
    target_closure_date: date | None = None
    priority: Priority | None = None
    bill_rate: Money | None = None
    pay_rate_min: Money | None = None
    pay_rate_max: Money | None = None
    currency: Currency | None = None
    rate_unit: RateUnit | None = None
    submission_limit: Annotated[int, Field(ge=1, le=1000)] | None = None
    # Epic 36 (AD-50): ``internal`` hides the demand from the public careers site (default ``external``).
    visibility: DemandVisibility | None = None
    interview_plan: Annotated[list[InterviewPlanStep], Field(max_length=20)] | None = None
    skills: Annotated[list[DemandSkillIn], Field(max_length=100)] | None = None
    mandatory_skills: Annotated[list[Text200], Field(max_length=100)] | None = Field(
        default=None, description="Shortcut: labels of mandatory skills (merged into `skills`)"
    )
    preferred_skills: Annotated[list[Text200], Field(max_length=100)] | None = Field(
        default=None, description="Shortcut: labels of preferred skills (merged into `skills`)"
    )

    @model_validator(mode="after")
    def _ranges(self) -> "_DemandFields":
        if (
            self.min_experience_years is not None
            and self.max_experience_years is not None
            and self.min_experience_years > self.max_experience_years
        ):
            raise ValueError("min_experience_years must be <= max_experience_years")
        if (
            self.pay_rate_min is not None
            and self.pay_rate_max is not None
            and self.pay_rate_min > self.pay_rate_max
        ):
            raise ValueError("pay_rate_min must be <= pay_rate_max")
        return self


class DemandCreate(_DemandFields):
    title: Text300
    client_id: uuid.UUID
    positions: Annotated[int, Field(ge=1, le=10000)] = 1


class DemandUpdate(_DemandFields):
    title: Text300 | None = None
    client_id: uuid.UUID | None = None
    positions: Annotated[int, Field(ge=1, le=10000)] | None = None


class DemandFilters(BaseModel):
    status: list[DemandStatus] | None = None
    client_id: uuid.UUID | None = None
    priority: list[Priority] | None = None
    assigned_to: str | None = Field(default=None, description="A user id, or `me`")
    owner_id: uuid.UUID | None = None
    q: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    sla_status: SlaStatus | None = None
    include_archived: bool = False
    sort: DemandSort = "-created_at"
    # Epic 11 dashboard drill-down filters: ``sla`` / ``assignee`` alias ``sla_status`` / ``assigned_to``;
    # ``opened_from`` / ``opened_to`` are local dates (tenant timezone) on ``opened_at``.
    sla: SlaStatus | None = None
    assignee: str | None = Field(default=None, description="A user id, or `me` (alias of assigned_to)")
    opened_from: date | None = None
    opened_to: date | None = None
    business_unit_ids: list[uuid.UUID] | None = None
    client_owner: Literal["me"] | None = Field(default=None, description="`me`: demands of clients I own")

    @field_validator("assigned_to", "assignee")
    @classmethod
    def _assigned_to(cls, value: str | None) -> str | None:
        if value is None or value == "me":
            return value
        uuid.UUID(value)
        return value


class DemandSkillOut(BaseModel):
    label: str
    skill_id: uuid.UUID | None
    skill_ref_kind: SkillRefKind | None
    skill_status: str | None = None
    kind: SkillKind
    min_years: Decimal | None
    min_proficiency: int | None = None  # Epic 37
    expand: bool = True  # Epic 37


class SlaClockOut(BaseModel):
    milestone: SlaMilestone
    ref_id: uuid.UUID | None
    status: SlaStatus
    started_at: datetime
    due_at: datetime
    completed_at: datetime | None


class PersonRef(BaseModel):
    id: uuid.UUID
    name: str | None


class DemandOut(BaseModel):
    id: uuid.UUID
    req_id: str
    title: str
    client_id: uuid.UUID
    client_name: str | None = None
    end_client: str | None
    hiring_manager_contact_id: uuid.UUID | None
    positions: int
    department: str | None
    employment_type: EmploymentType | None
    country: str | None
    location: str | None
    work_mode: WorkMode | None
    min_experience_years: Decimal | None
    max_experience_years: Decimal | None
    education: str | None
    certifications: list[str]
    work_authorization: list[str]
    clearance: str | None
    languages: list[str]
    shift: str | None
    travel_percent: int | None
    start_date: date | None
    target_closure_date: date | None
    priority: Priority
    bill_rate: Decimal | None
    pay_rate_min: Decimal | None
    pay_rate_max: Decimal | None
    margin_percent: Decimal | None = None
    currency: str | None
    rate_unit: RateUnit | None
    submission_limit: int | None
    interview_plan: list[dict[str, Any]]
    status: DemandStatus
    status_reason_code: str | None
    status_comment: str | None
    progress: Progress | None
    owner_id: uuid.UUID
    business_unit_id: uuid.UUID | None
    ai_extracted_fields: list[str]
    intake_ai_action_id: uuid.UUID | None
    published_jd_version_id: uuid.UUID | None
    applications_count: int
    submissions_count: int
    interviews_scheduled_count: int
    offers_released_count: int
    positions_filled: int
    opened_at: datetime | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Epic 36: careers visibility and Phase 3 links (read only; set via ``demands_service.create_draft``).
    visibility: DemandVisibility = "external"
    workforce_plan_line_id: uuid.UUID | None = None
    project_role_id: uuid.UUID | None = None
    skills: list[DemandSkillOut] = Field(default_factory=list)
    primary_recruiter: PersonRef | None = None
    sla: list[SlaClockOut] = Field(default_factory=list)
    sla_status: SlaStatus | None = None
    masked_fields: list[str] = Field(default_factory=list)


class TransitionIn(_In):
    to: DemandStatus
    reason_code: Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)] | None = None
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] | None = None


class ConfirmAiFieldsIn(_In):
    fields: list[str] | None = Field(default=None, description="Fields to confirm; omit to confirm all")


class ReasonCodesOut(BaseModel):
    on_hold: list[str]
    cancelled: list[str]
    closed: list[str]


# ================================================================ JD / bias / posting
class JdVersionIn(_In):
    kind: JdKind = "full"
    content: Annotated[str, StringConstraints(min_length=1, max_length=20000)]


class JdGenerateIn(_In):
    kind: JdKind = "full"
    show_rate: bool = False


class JdVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    demand_id: uuid.UUID
    version_no: int
    kind: JdKind
    content: str
    author_type: Literal["user", "ai"]
    author_id: uuid.UUID | None
    ai_action_id: uuid.UUID | None
    restored_from_id: uuid.UUID | None
    is_published: bool = False
    created_at: datetime


class BiasMatchOut(BaseModel):
    term: str
    category: str
    start: int
    end: int


class BiasCheckOut(BaseModel):
    matches: list[BiasMatchOut]


class BiasedTermIn(_In):
    term: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    category: Literal[
        "age", "gender", "marital", "religion", "caste", "nationality", "disability", "appearance", "other"
    ] = "other"
    enabled: bool = True


class BiasedTermOut(BaseModel):
    id: uuid.UUID | None
    term: str
    category: str
    enabled: bool
    source: Literal["default", "tenant"]


class PostingIn(_In):
    jd_version_id: uuid.UUID
    show_rate: bool = False
    show_location: bool = True
    show_client_name: bool = False
    acknowledge_bias_flags: bool = False


class PostingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    demand_id: uuid.UUID
    jd_version_id: uuid.UUID
    show_rate: bool
    show_location: bool
    show_client_name: bool
    is_live: bool
    published_at: datetime | None
    unpublished_at: datetime | None


class LivePosting(BaseModel):
    """For the Career Portal (Epic 7). ``client_name`` is None unless shown; rate only when shown."""

    demand_id: uuid.UUID
    req_id: str
    title: str
    content: str
    location: str | None
    country: str | None
    work_mode: str | None
    employment_type: str | None
    client_name: str | None
    bill_rate: Decimal | None
    currency: str | None
    rate_unit: str | None
    published_at: datetime | None


class DemandDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: Literal["intake_source", "attachment"]
    file_name: str
    content_type: str
    size: int
    scan_status: str
    file_id: uuid.UUID | None
    created_at: datetime


# ================================================================ assignment
class AssignmentsIn(_In):
    user_ids: Annotated[list[uuid.UUID], Field(max_length=100)] = Field(default_factory=list)
    team_ids: Annotated[list[uuid.UUID], Field(max_length=50)] = Field(default_factory=list)
    primary_user_id: uuid.UUID | None = None


class AssignmentOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    team_id: uuid.UUID | None
    name: str | None
    is_primary: bool
    assigned_by: uuid.UUID | None
    assigned_at: datetime
    first_activity_at: datetime | None
    first_application_at: datetime | None
    first_submission_at: datetime | None


class AssignmentsOut(BaseModel):
    items: list[AssignmentOut]


# ================================================================ settings
class SlaTargetIn(_In):
    milestone: SlaMilestone
    priority: Priority | None = None
    target_hours: Annotated[Decimal, Field(gt=0, le=100000, max_digits=8, decimal_places=2)]
    clock: SlaClockType = "business"
    at_risk_percent: Annotated[int, Field(ge=1, le=99)] = 75


class SlaTargetsIn(_In):
    targets: Annotated[list[SlaTargetIn], Field(max_length=40)]


class SlaTargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    milestone: SlaMilestone
    priority: Priority | None
    target_hours: Decimal
    clock: SlaClockType
    at_risk_percent: int


class WorkWindow(_In):
    start: Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
    end: Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$|^24:00$")]


class BusinessCalendarIn(_In):
    work_week: dict[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"], WorkWindow]
    holidays: Annotated[list[date], Field(max_length=400)] = Field(default_factory=list)


class BusinessCalendarOut(BaseModel):
    work_week: dict[str, dict[str, str]]
    holidays: list[date]
    timezone: str


class DemandSettingsIn(_In):
    req_prefix: str = Field(description="1-10 characters: a letter, then letters or digits (upper-cased)")

    @field_validator("req_prefix")
    @classmethod
    def _prefix(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}", value):
            raise ValueError("must be a letter followed by up to 9 letters or digits")
        return value


class DemandSettingsOut(BaseModel):
    req_prefix: str


# ================================================================ AI output schemas
class JdDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Annotated[str, StringConstraints(min_length=1, max_length=20000)]


class IntakeExtraction(BaseModel):
    """AI intake output (AD-6: schema-forced, ``extra`` forbidden). Loosely typed on purpose: invalid values
    are dropped field by field by the intake service instead of failing the whole extraction."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    mandatory_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    min_experience_years: float | None = None
    max_experience_years: float | None = None
    positions: int | None = None
    location: str | None = None
    country: str | None = None
    work_mode: str | None = None
    bill_rate: float | None = None
    pay_rate_min: float | None = None
    pay_rate_max: float | None = None
    currency: str | None = None
    rate_unit: str | None = None
    start_date: str | None = None
    employment_type: str | None = None
    certifications: list[str] = Field(default_factory=list)
