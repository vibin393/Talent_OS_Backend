"""Candidate API schemas (Epic 3). JSON is snake_case; compensation fields are named ``*_ctc_*`` so
``mask_compensation`` hides them from principals without ``compensation:view``."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.schemas.files import StoredFileOut

CandidateSource = Literal[
    "manual", "resume_upload", "bulk_import", "career_portal", "referral", "job_board", "other",
    # Phase 3 (Epic 36 300_01): campus/event registrations and internal mobility
    "campus", "event", "internal",
    # Phase 5 (Epic 66 500_01 widened the CHECK; Epic 67 Public API writes ``public_api``)
    "job_network", "public_api",
]
CtcUnit = Literal["annual", "monthly", "daily", "hourly"]
LinkKind = Literal["linkedin", "github", "portfolio", "other"]
SkillKind = Literal["global", "tenant"]
SkillSource = Literal["parsed", "manual"]
ParseStatus = Literal["parsed", "failed_manual", "unavailable", "skipped_disabled", "skipped_consent"]
TextStatus = Literal["ok", "image_only", "encrypted", "failed"]
DuplicateStatus = Literal["open", "merged", "kept_separate"]
ImportItemStatus = Literal["queued", "processing", "created", "linked", "needs_review", "failed"]

Str200 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Currency = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z]{3}$")]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ nested inputs
class LocationIn(_In):
    city: Str200 | None = None
    state: Str200 | None = None
    country: Str200 | None = None


class EmailIn(_In):
    email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=320)]
    is_primary: bool = False


class PhoneIn(_In):
    phone: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=40)]
    is_primary: bool = False


class LinkIn(_In):
    url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=1000)]
    kind: LinkKind | None = None


class EmploymentIn(_In):
    employer: Str200 | None = None
    title: Str200 | None = None
    location: Str200 | None = None
    start_date: date | None = None
    end_date: date | None = None
    is_current: bool = False
    description: Annotated[str, StringConstraints(max_length=10000)] | None = None


class EducationIn(_In):
    institution: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    degree: Str200 | None = None
    field: Str200 | None = None
    start_year: int | None = Field(default=None, ge=1900, le=2200)
    end_year: int | None = Field(default=None, ge=1900, le=2200)
    grade: Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)] | None = None


class CertificationIn(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    issuer: Str200 | None = None
    issued_on: date | None = None
    expires_on: date | None = None
    credential_id: Str200 | None = None


class SkillRef(BaseModel):
    kind: SkillKind
    id: uuid.UUID
    name: str
    status: str = "approved"


class SkillRefIn(_In):
    kind: SkillKind
    id: uuid.UUID


class CandidateSkillIn(_In):
    """A skill by free text (``name``, resolved via the catalog) or by catalog reference (``ref``)."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    ref: SkillRefIn | None = None
    years: Decimal | None = Field(default=None, ge=0, le=999)
    last_used: date | None = None
    source: SkillSource = "manual"


SkillInput = (
    Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | CandidateSkillIn
)


class _CandidateFields(_In):
    location: LocationIn | None = None
    current_title: Str200 | None = None
    current_employer: Str200 | None = None
    total_experience_months: int | None = Field(default=None, ge=0, le=1200)
    current_ctc_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    current_ctc_currency: Currency | None = None
    current_ctc_unit: CtcUnit | None = None
    expected_ctc_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    expected_ctc_currency: Currency | None = None
    expected_ctc_unit: CtcUnit | None = None
    availability_date: date | None = None
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    last_working_date: date | None = None
    work_authorization: Str200 | None = None
    willing_to_relocate: bool | None = None
    preferred_locations: list[LocationIn] | None = None
    source_detail: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    owner_id: uuid.UUID | None = None


class CandidateCreate(_CandidateFields):
    full_name: Name
    source: CandidateSource | None = None
    emails: list[EmailIn] = Field(default_factory=list, max_length=20)
    phones: list[PhoneIn] = Field(default_factory=list, max_length=20)
    links: list[LinkIn] = Field(default_factory=list, max_length=20)
    employment: list[EmploymentIn] = Field(default_factory=list, max_length=50)
    education: list[EducationIn] = Field(default_factory=list, max_length=30)
    certifications: list[CertificationIn] = Field(default_factory=list, max_length=50)
    skills: list[SkillInput] = Field(default_factory=list, max_length=200)
    tags: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]] = Field(
        default_factory=list, max_length=50
    )
    resume_id: uuid.UUID | None = None


class CandidatePrefill(CandidateCreate):
    """``CandidateCreate`` pre-filled from a parse; the name may be missing (the user completes it)."""

    full_name: Str200 | None = None  # type: ignore[assignment]


class CandidateUpdate(_CandidateFields):
    """Partial update. Any child list that is provided replaces the existing rows."""

    full_name: Name | None = None
    source: CandidateSource | None = None
    emails: list[EmailIn] | None = Field(default=None, max_length=20)
    phones: list[PhoneIn] | None = Field(default=None, max_length=20)
    links: list[LinkIn] | None = Field(default=None, max_length=20)
    employment: list[EmploymentIn] | None = Field(default=None, max_length=50)
    education: list[EducationIn] | None = Field(default=None, max_length=30)
    certifications: list[CertificationIn] | None = Field(default=None, max_length=50)
    skills: list[SkillInput] | None = Field(default=None, max_length=200)


class CandidateSkillsSet(_In):
    skills: list[SkillInput] = Field(max_length=200)


# ================================================================ outputs
class LocationOut(BaseModel):
    city: str | None = None
    state: str | None = None
    country: str | None = None


class EmailOut(BaseModel):
    id: uuid.UUID
    email: str
    is_primary: bool


class PhoneOut(BaseModel):
    id: uuid.UUID
    phone_e164: str
    is_primary: bool


class LinkOut(BaseModel):
    id: uuid.UUID
    url_norm: str
    kind: LinkKind


class EmploymentOut(BaseModel):
    id: uuid.UUID
    employer: str | None
    title: str | None
    location: str | None
    start_date: date | None
    end_date: date | None
    is_current: bool
    description: str | None


class EducationOut(BaseModel):
    id: uuid.UUID
    institution: str | None
    degree: str | None
    field: str | None
    start_year: int | None
    end_year: int | None
    grade: str | None


class CertificationOut(BaseModel):
    id: uuid.UUID
    name: str
    issuer: str | None
    issued_on: date | None
    expires_on: date | None
    credential_id: str | None


class CandidateSkillOut(BaseModel):
    id: uuid.UUID
    skill: SkillRef
    source: SkillSource
    years: Decimal | None
    last_used: date | None
    # Epic 36 (AD-41): read only; written by ``skills_service.recompute`` (Epic 37).
    proficiency: int | None = None
    verification: Literal["declared", "inferred", "verified"] = "declared"


class TagOut(BaseModel):
    id: uuid.UUID
    name: str
    color: str | None


class ListRefOut(BaseModel):
    id: uuid.UUID
    name: str


class ResumeOut(BaseModel):
    id: uuid.UUID
    candidate_id: uuid.UUID | None
    version: int | None
    text_status: TextStatus
    parse_status: ParseStatus
    parse_label: str
    ai_action_id: uuid.UUID | None
    uploaded_by: uuid.UUID | None
    created_at: datetime
    file: StoredFileOut


class CandidateOut(BaseModel):
    """List row / summary. Compensation fields are ``None`` when masked."""

    id: uuid.UUID
    human_id: str
    full_name: str
    current_title: str | None
    current_employer: str | None
    location: LocationOut | None
    total_experience_months: int | None
    source: CandidateSource
    owner_id: uuid.UUID
    owner_name: str | None = None
    primary_email: str | None = None
    primary_phone: str | None = None
    archived_at: datetime | None
    merged_into_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    # Epic 12 (FR-67): "AI consent pending" flag, maintained by the privacy service.
    ai_consent_state: Literal["granted", "pending", "withdrawn", "not_required"] = "pending"


class CandidateCounts(BaseModel):
    resumes: int
    attachments: int
    skills: int
    tags: int
    lists: int
    open_duplicates: int


class Candidate360Out(CandidateOut):
    current_ctc_amount: Decimal | None = None
    current_ctc_currency: str | None = None
    current_ctc_unit: CtcUnit | None = None
    expected_ctc_amount: Decimal | None = None
    expected_ctc_currency: str | None = None
    expected_ctc_unit: CtcUnit | None = None
    masked_fields: list[str] = Field(default_factory=list)
    availability_date: date | None
    notice_period_days: int | None
    last_working_date: date | None
    work_authorization: str | None
    willing_to_relocate: bool | None
    preferred_locations: list[LocationOut] | None
    source_detail: str | None
    emails: list[EmailOut]
    phones: list[PhoneOut]
    links: list[LinkOut]
    employment: list[EmploymentOut]
    education: list[EducationOut]
    certifications: list[CertificationOut]
    skills: list[CandidateSkillOut]
    tags: list[TagOut]
    lists: list[ListRefOut]
    resumes: list[ResumeOut]
    counts: CandidateCounts
    applications: list[Any] = Field(default_factory=list, description="Placeholder until the pipeline epic")
    can_edit: bool = False


class CandidateWarnings(BaseModel):
    possible_duplicates: list[CandidateOut] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list, description="Skills that were skipped (e.g. rejected)")


class CandidateCreatedOut(Candidate360Out):
    warnings: CandidateWarnings = Field(default_factory=CandidateWarnings)


class CandidateFilters(BaseModel):
    q: str | None = Field(default=None, max_length=200)
    owner_id: uuid.UUID | None = None
    tag_id: uuid.UUID | None = None
    list_id: uuid.UUID | None = None
    source: CandidateSource | None = None
    ai_consent_state: Literal["granted", "pending", "withdrawn", "not_required"] | None = None
    include_archived: bool = False
    sort: Literal["name", "-name", "created_at", "-created_at", "updated_at", "-updated_at"] = "-created_at"


# ================================================================ skills catalog
class SkillOut(SkillRef):
    category: str | None = None
    parent_name: str | None = None
    aliases: list[str] = Field(default_factory=list)


class SkillCreate(_In):
    name: Name
    category: Str200 | None = None
    parent_skill_id: uuid.UUID | None = None
    aliases: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]] = (
        Field(default_factory=list, max_length=50)
    )


class SkillAliasCreate(_In):
    alias: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class SkillReject(_In):
    merge_into: SkillRefIn | None = None


class PendingSkillOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    category: str | None
    status: str
    created_from: str
    created_at: datetime
    candidate_count: int


# ================================================================ resumes / parsing
class ParsedLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str | None = None
    state: str | None = None
    country: str | None = None


class ParsedLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    kind: LinkKind | None = None


class ParsedSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    years: float | None = None
    last_used: str | None = None


class ParsedEmployment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employer: str | None = None
    title: str | None = None
    location: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = False
    description: str | None = None


class ParsedProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    technologies: list[str] = Field(default_factory=list)
    role: str | None = None


class ParsedEducation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    institution: str | None = None
    degree: str | None = None
    field: str | None = None
    start_year: int | None = None
    end_year: int | None = None
    grade: str | None = None


class ParsedCertification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    issuer: str | None = None
    issued_on: str | None = None
    expires_on: str | None = None
    credential_id: str | None = None


class ResumeParseOutput(BaseModel):
    """Tool schema the model must fill (``extra="forbid"`` everywhere: unknown keys = invalid output)."""

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = None
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    location: ParsedLocation | None = None
    links: list[ParsedLink] = Field(default_factory=list)
    current_title: str | None = None
    current_employer: str | None = None
    total_experience_months: int | None = None
    skills: list[ParsedSkill] = Field(default_factory=list)
    employment: list[ParsedEmployment] = Field(default_factory=list)
    projects: list[ParsedProject] = Field(default_factory=list)
    education: list[ParsedEducation] = Field(default_factory=list)
    certifications: list[ParsedCertification] = Field(default_factory=list)
    limitations_note: str | None = None


class UnresolvedSkill(BaseModel):
    name: str
    years: Decimal | None = None
    last_used: date | None = None


class ResumeDraftOut(BaseModel):
    resume: ResumeOut
    parse_status: ParseStatus
    prefill: CandidatePrefill
    ai_fields: list[str] = Field(default_factory=list, description="Prefill fields that came from AI parsing")
    unresolved_skills: list[UnresolvedSkill] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    possible_duplicates: list[CandidateOut] = Field(default_factory=list)


class DiffEntry(BaseModel):
    field: str
    index: int | None = None
    current: Any = None
    parsed: Any = None


class ResumeUploadOut(BaseModel):
    resume: ResumeOut
    parse_status: ParseStatus
    diff: list[DiffEntry]
    unresolved_skills: list[UnresolvedSkill] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ResumeApply(_In):
    fields: list[str] = Field(default_factory=list, description="Scalar fields to take from the parse")
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    employment_idx: list[int] = Field(default_factory=list)
    education_idx: list[int] = Field(default_factory=list)
    certification_idx: list[int] = Field(default_factory=list)

    @field_validator("fields")
    @classmethod
    def _known_fields(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(SCALAR_DIFF_FIELDS))
        if unknown:
            msg = f"unknown fields: {', '.join(unknown)}"
            raise ValueError(msg)
        return value


SCALAR_DIFF_FIELDS = ("full_name", "current_title", "current_employer", "total_experience_months", "location")


class ResumeApplyOut(BaseModel):
    candidate: Candidate360Out
    decision: Literal["accepted", "edited", "rejected"] | None


# ================================================================ duplicates / merges
class DuplicateOut(BaseModel):
    id: uuid.UUID
    candidate_a: CandidateOut
    candidate_b: CandidateOut
    reasons: list[str]
    score: Decimal
    status: DuplicateStatus
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    created_at: datetime


class DuplicateDetailOut(DuplicateOut):
    profile_a: Candidate360Out
    profile_b: Candidate360Out
    differing_fields: list[str]


class MergeIn(_In):
    survivor_id: uuid.UUID
    field_choices: dict[str, Literal["a", "b"]] = Field(default_factory=dict)


class MergeOut(BaseModel):
    id: uuid.UUID
    survivor_id: uuid.UUID
    merged_id: uuid.UUID
    duplicate_id: uuid.UUID | None
    field_choices: dict[str, str]
    merged_by: uuid.UUID | None
    merged_at: datetime
    undo_until: datetime
    undone_at: datetime | None
    can_undo: bool


# ================================================================ imports
class ImportItemOut(BaseModel):
    id: uuid.UUID
    filename: str
    file_id: uuid.UUID | None
    resume_id: uuid.UUID | None = Field(default=None, description="Draft resume for 'create manually'")
    status: ImportItemStatus
    reason: str | None
    candidate_id: uuid.UUID | None
    duplicate_id: uuid.UUID | None
    updated_at: datetime


class ImportBatchOut(BaseModel):
    id: uuid.UUID
    status: Literal["queued", "processing", "completed"]
    total: int
    counts: dict[str, int]
    created_by: uuid.UUID | None
    created_at: datetime
    completed_at: datetime | None


class ImportItemsPage(BaseModel):
    items: list[ImportItemOut]
    total: int
    page: int
    page_size: int


class ImportBatchDetailOut(ImportBatchOut):
    items: ImportItemsPage


# ================================================================ tags / lists
class TagNamesIn(_In):
    names: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]] = (
        Field(min_length=1, max_length=50)
    )


class BulkTagsIn(TagNamesIn):
    candidate_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class BulkTagsOut(BaseModel):
    tags: list[TagOut]
    updated: int


class CandidateListCreate(_In):
    name: Name
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None


class CandidateListUpdate(_In):
    name: Name | None = None
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None


class ListSharesIn(_In):
    user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    team_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class ListMembersIn(_In):
    candidate_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class CandidateListOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    owner_id: uuid.UUID
    archived_at: datetime | None
    member_count: int
    shared_user_ids: list[uuid.UUID]
    shared_team_ids: list[uuid.UUID]
    is_owner: bool
    created_at: datetime
    updated_at: datetime
