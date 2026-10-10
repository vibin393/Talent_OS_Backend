"""Internal BGV and reference-check DTOs (Epic 20). Portal DTOs live in ``bgv_portal.py``."""

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

CheckType = Literal[
    "identity",
    "employment",
    "education",
    "address",
    "reference",
    "professional_licence",
    "criminal_record",
    "drug_test",
]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]
DocKey = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_]{1,59}$")]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ================================================================ configuration
class PackageCheckIn(_In):
    type: CheckType
    count: int = Field(default=1, ge=1, le=10)
    required_documents: list[DocKey] = Field(default_factory=list, max_length=20)
    turnaround_days: int = Field(default=5, ge=1, le=90)


class PackageIn(_In):
    name: Name
    description: Text | None = None
    checks: list[PackageCheckIn] = Field(min_length=1, max_length=30)
    turnaround_target_days: int | None = Field(default=None, ge=1, le=180)
    lawful_confirmed: bool = False


class PackageUpdate(_In):
    name: Name | None = None
    description: Text | None = None
    checks: list[PackageCheckIn] | None = Field(default=None, min_length=1, max_length=30)
    turnaround_target_days: int | None = Field(default=None, ge=1, le=180)
    lawful_confirmed: bool | None = None


class PackageOut(_Out):
    id: uuid.UUID
    name: str
    description: str | None
    checks: list[dict[str, Any]]
    turnaround_target_days: int | None
    lawful_confirmed_by: uuid.UUID | None
    lawful_confirmed_at: datetime | None
    archived_at: datetime | None
    created_at: datetime


class ProviderIn(_In):
    name: Name
    kind: Literal["internal", "external"] = "external"
    contact_name: Name | None = None
    contact_email: EmailStr | None = None
    supported_types: list[CheckType] = Field(default_factory=list)
    connector_config_id: uuid.UUID | None = None
    active: bool = True


class ProviderUpdate(_In):
    name: Name | None = None
    contact_name: Name | None = None
    contact_email: EmailStr | None = None
    supported_types: list[CheckType] | None = None
    connector_config_id: uuid.UUID | None = None
    active: bool | None = None


class ProviderOut(_Out):
    id: uuid.UUID
    name: str
    kind: str
    contact_name: str | None
    contact_email: str | None
    supported_types: list[str]
    connector_config_id: uuid.UUID | None
    active: bool


class QuestionIn(_In):
    key: DocKey
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]
    kind: Literal["text", "rating", "yes_no"] = "text"
    required: bool = True


class QuestionnaireIn(_In):
    name: Name
    questions: list[QuestionIn] = Field(min_length=1, max_length=30)
    is_default: bool = False

    @field_validator("questions")
    @classmethod
    def _unique(cls, v: list[QuestionIn]) -> list[QuestionIn]:
        if len({q.key for q in v}) != len(v):
            msg = "question keys must be unique"
            raise ValueError(msg)
        return v


class QuestionnaireUpdate(_In):
    name: Name | None = None
    questions: list[QuestionIn] | None = Field(default=None, min_length=1, max_length=30)
    is_default: bool | None = None
    archived: bool | None = None


class QuestionnaireOut(_Out):
    id: uuid.UUID
    name: str
    questions: list[dict[str, Any]]
    is_default: bool
    archived_at: datetime | None


class BgvSettingsOut(_Out):
    doc_reminder_every_days: int
    discrepancy_response_working_days: int
    reference_link_days: int
    reference_reminder_days: int
    reuse_clear_case_days: int
    provider_link_days: int


class BgvSettingsUpdate(_In):
    doc_reminder_every_days: int | None = Field(default=None, ge=1, le=30)
    discrepancy_response_working_days: int | None = Field(default=None, ge=1, le=30)
    reference_link_days: int | None = Field(default=None, ge=1, le=60)
    reference_reminder_days: int | None = Field(default=None, ge=1, le=30)
    reuse_clear_case_days: int | None = Field(default=None, ge=0, le=730)
    provider_link_days: int | None = Field(default=None, ge=1, le=90)


# ================================================================ cases
class CaseCreateIn(_In):
    package_id: uuid.UUID
    placement_id: uuid.UUID | None = None


class CancelIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=2000)]


class AdjudicateIn(_In):
    outcome: Literal["clear", "failed"]
    with_exceptions: bool = False
    reasons: Annotated[str, StringConstraints(strip_whitespace=True, min_length=20, max_length=4000)]


class DocumentRequestOut(_Out):
    id: uuid.UUID
    key: str
    label: str
    kind: str
    required: bool
    status: str
    file_ids: list[uuid.UUID]
    declared: dict[str, Any] | None
    return_note: str | None
    redaction_confirmed_by: uuid.UUID | None
    submitted_at: datetime | None


class DocumentReviewIn(_In):
    action: Literal["accept", "return"]
    note: Text | None = None
    redaction_confirmed: bool = False


class CheckOut(_Out):
    id: uuid.UUID
    case_id: uuid.UUID
    type: str
    seq: int
    subject_label: str
    document_request_keys: list[str]
    turnaround_days: int | None
    status: str
    halted_reason: str | None
    assignee_kind: str | None
    assignee_user_id: uuid.UUID | None
    provider_id: uuid.UUID | None
    provider_name: str | None = None
    external_ref: str | None
    connector_job_id: uuid.UUID | None
    started_at: datetime | None
    due_on: date | None
    overdue: bool
    result_note: str | None
    verifier_name: str | None
    source_contacted: str | None
    verified_on: date | None
    recorded_by: uuid.UUID | None
    recorded_at: datetime | None
    waived_reason: str | None
    provider_proposal: dict[str, Any] | None
    reference_check_id: uuid.UUID | None
    evidence_file_ids: list[uuid.UUID] = Field(default_factory=list)
    provider_link_active: bool = False


class DiscrepancyOut(_Out):
    id: uuid.UUID
    case_id: uuid.UUID
    check_id: uuid.UUID
    severity: str
    description: str
    internal_note: str | None
    status: str
    response_due_on: date
    candidate_response: str | None
    response_file_ids: list[uuid.UUID]
    responded_at: datetime | None
    resolution_note: str | None
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    created_at: datetime


class CaseEventOut(_Out):
    id: uuid.UUID
    kind: str
    from_status: str | None
    to_status: str | None
    actor_kind: str
    actor_id: uuid.UUID | None
    detail: dict[str, Any]
    at: datetime


class CaseListItem(BaseModel):
    id: uuid.UUID
    human_id: str
    candidate_id: uuid.UUID
    candidate_name: str | None
    placement_id: uuid.UUID | None
    client_id: uuid.UUID | None
    package_id: uuid.UUID
    package_name: str | None
    required: bool
    status: str
    outcome_qualifier: str | None
    overdue_checks: int
    open_discrepancies: int
    created_at: datetime
    age_days: int


class CaseOut(CaseListItem):
    application_id: uuid.UUID | None
    consent_active: bool
    consent_withdrawn_at: datetime | None
    adjudication_reasons: str | None
    adjudicated_by: uuid.UUID | None
    adjudicated_at: datetime | None
    cancel_reason: str | None
    evidence_purge_after: date | None
    evidence_purged_at: datetime | None
    started_at: datetime | None
    documents: list[DocumentRequestOut]
    checks: list[CheckOut]
    discrepancies: list[DiscrepancyOut]
    discrepancy_awaiting_response: bool
    lock_version: int


class CaseSummaryOut(BaseModel):
    """Placement tab / Worker 360 section."""

    id: uuid.UUID
    human_id: str
    package_name: str | None
    required: bool
    status: str
    outcome_qualifier: str | None
    adjudicated_at: datetime | None


# ================================================================ checks
class AssignIn(_In):
    assignee_kind: Literal["internal", "provider"]
    assignee_user_id: uuid.UUID | None = None
    provider_id: uuid.UUID | None = None


class DiscrepancyIn(_In):
    severity: Literal["minor", "major", "critical"]
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=4000)]
    internal_note: Text | None = None


class ResultIn(_In):
    result: Literal["verified", "discrepancy", "unable_to_verify"]
    note: Text | None = None
    verifier_name: Name | None = None
    source_contacted: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    verified_on: date | None = None
    evidence_file_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    accept_proposal: bool = False
    discrepancy: DiscrepancyIn | None = None


class WaiveIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=2000)]


class ResolveIn(_In):
    resolution_note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=4000)]


class EvidenceOut(BaseModel):
    file_id: uuid.UUID
    original_name: str
    uploaded_by_kind: str


# ================================================================ references
class ReferenceRequestIn(_In):
    questionnaire_id: uuid.UUID | None = None
    min_referees: int = Field(default=2, ge=1, le=10)
    case_id: uuid.UUID | None = None
    check_id: uuid.UUID | None = None


class ReviewIn(_In):
    note: Text | None = None


class RefereeOut(_Out):
    id: uuid.UUID
    name: str
    relationship: str | None
    company: str | None
    email: str
    phone: str | None
    status: str
    sent_at: datetime | None
    expires_at: datetime | None
    responded_at: datetime | None


class ReferenceCheckOut(_Out):
    id: uuid.UUID
    candidate_id: uuid.UUID
    case_id: uuid.UUID | None
    check_id: uuid.UUID | None
    requested_by: uuid.UUID | None
    min_referees: int
    status: str
    questionnaire_snapshot: dict[str, Any]
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    review_note: str | None
    created_at: datetime
    referees: list[RefereeOut] = Field(default_factory=list)


class ReferenceAnswerOut(BaseModel):
    referee_id: uuid.UUID
    referee_name: str
    relationship: str | None
    company: str | None
    submitted_at: datetime
    answers: dict[str, Any]


class ReferenceResponsesOut(BaseModel):
    reference_check_id: uuid.UUID
    questions: list[dict[str, Any]]
    responses: list[ReferenceAnswerOut]


# ================================================================ public links
class ProviderCheckView(BaseModel):
    """``GET /public/links/{token}/bgv-check``: minimal (first name + last initial, type, subject)."""

    tenant_name: str | None
    check_type: str
    subject_label: str
    candidate_name: str | None
    files: list[EvidenceOut]
    declarations: dict[str, Any]
    submitted: bool


class ProviderSubmitIn(_In):
    result: Literal["verified", "discrepancy", "unable_to_verify"]
    note: Text | None = None
    verifier_name: Name | None = None
    source_contacted: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    verified_on: date | None = None


class ReferenceFormView(BaseModel):
    tenant_name: str | None
    candidate_name: str | None
    referee_name: str | None
    questions: list[dict[str, Any]]
    status: str


class ReferenceSubmitIn(_In):
    answers: dict[str, Any]
