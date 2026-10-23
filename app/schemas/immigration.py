# ruff: file-ignore[line-too-long]
"""Immigration DTOs (Epic 46, FR-232..FR-236, AD-52).

``VisaTypeDefinition`` is shared by the platform templates and the tenant versions. Field-level shapes are checked
here; the business rules with their own error codes (``visa_stage_category_invalid`` ...) live in
``services/immigration/definition.py`` so the API returns those codes instead of a generic 422.

Encrypted numbers (receipt, permit, dependant passport) only ever appear as ``*_last4`` (masked ``••••1234`` in
the UI); the plaintext is returned once by the audited unmask endpoints (``UnmaskOut``).
"""

import datetime as _dt
import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DISCLAIMER = "Operational checklist, not legal advice — verify with your counsel before use."

CaseStatus = Literal["open", "in_stage", "approved", "refused", "withdrawn"]
StageCategory = Literal["preparing", "filed", "pending_decision"]
VisaCategory = Literal["work", "dependent", "business_travel"]
CollectFrom = Literal["worker", "counsel", "internal"]
RequestStatus = Literal["requested", "uploaded", "verified", "rejected", "waived"]


# ================================================================ definition
class _Loose(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidityRule(_Loose):
    min_months_remaining: int | None = Field(default=None, ge=0, le=120)
    max_age_days: int | None = Field(default=None, ge=1, le=3650)


class StageDef(_Loose):
    key: str = Field(min_length=1, max_length=60, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=200)
    category: str
    sla_days: int | None = Field(default=None, ge=1, le=730)
    help: str | None = Field(default=None, max_length=2000)


class ChecklistItemDef(_Loose):
    key: str = Field(min_length=1, max_length=60, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=200)
    document_category: str = Field(min_length=1, max_length=40)
    mandatory: bool = True
    applies_to: Literal["principal", "dependant", "employer", "counsel"] = "principal"
    collect_from: CollectFrom = "worker"
    validity_rule: ValidityRule | None = None
    help: str | None = Field(default=None, max_length=2000)


class ExpiryFieldDef(_Loose):
    key: str = Field(min_length=1, max_length=60, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=200)
    primary: bool = False
    required: bool = False


class KeyDateDef(_Loose):
    key: str = Field(min_length=1, max_length=60, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=200)


class VisaTypeDefinition(_Loose):
    stages: list[StageDef] = Field(min_length=1, max_length=40)
    checklist: list[ChecklistItemDef] = Field(default_factory=list, max_length=100)
    expiry_fields: list[ExpiryFieldDef] = Field(min_length=1, max_length=10)
    alert_offsets: list[int] = Field(default_factory=lambda: [120, 90, 60, 30], max_length=10)
    key_dates: list[KeyDateDef] = Field(default_factory=list, max_length=30)


# ================================================================ templates / visa types
class VisaTemplateOut(BaseModel):
    key: str
    country: str
    name: str
    category: str
    template_version: int
    last_reviewed: date
    notice: str
    definition: VisaTypeDefinition
    adopted_visa_type_id: uuid.UUID | None = None


class AdoptIn(_Loose):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    key: str | None = Field(default=None, min_length=2, max_length=50, pattern=r"^[a-z][a-z0-9_]*$")


class VisaTypeCreate(_Loose):
    key: str = Field(min_length=2, max_length=50, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=200)
    country: str = Field(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")
    category: VisaCategory = "work"
    definition: dict[str, Any]
    last_reviewed: date | None = None


class VisaTypePatch(_Loose):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    active: bool | None = None
    last_reviewed: date | None = None


class DefinitionIn(_Loose):
    definition: dict[str, Any]


class VisaTypeVersionOut(BaseModel):
    id: uuid.UUID
    version_no: int
    created_by: uuid.UUID | None
    created_at: datetime


class VisaTypeOut(BaseModel):
    id: uuid.UUID
    key: str
    country: str
    name: str
    category: str
    active: bool
    archived_at: datetime | None
    template_key: str | None
    template_version: int | None
    template_last_reviewed: date | None
    last_reviewed: date | None
    disclaimer: str = DISCLAIMER
    current_version_id: uuid.UUID | None
    current_version_no: int | None
    credential_type_key: str
    open_cases: int = 0


class VisaTypeDetailOut(VisaTypeOut):
    definition: VisaTypeDefinition
    versions: list[VisaTypeVersionOut]


# ================================================================ counsel
class CounselIn(_Loose):
    name: str = Field(min_length=1, max_length=200)
    firm: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=4000)


class CounselPatch(_Loose):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    firm: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=4000)
    archived: bool | None = None


class CounselOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    firm: str | None
    email: str | None
    phone: str | None
    notes: str | None
    archived_at: datetime | None


# ================================================================ cases
class CaseCreate(_Loose):
    candidate_id: uuid.UUID
    visa_type_id: uuid.UUID
    owner_user_id: uuid.UUID | None = None
    placement_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    demand_id: uuid.UUID | None = None
    counsel_id: uuid.UUID | None = None
    sponsoring_legal_entity_id: uuid.UUID | None = None


class CasePatch(_Loose):
    owner_user_id: uuid.UUID | None = None
    placement_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    demand_id: uuid.UUID | None = None
    counsel_id: uuid.UUID | None = None
    sponsoring_legal_entity_id: uuid.UUID | None = None
    lock_version: int | None = None


class StageMoveIn(_Loose):
    stage_key: str = Field(min_length=1, max_length=60)
    reason: str | None = Field(default=None, max_length=2000)


class KeyDateIn(_Loose):
    key: str = Field(min_length=1, max_length=60)
    date: _dt.date | None = None


class ReceiptIn(_Loose):
    number: str = Field(min_length=3, max_length=40)


class PermitIn(_Loose):
    permit_type: str | None = Field(default=None, min_length=1, max_length=200)
    number: str = Field(min_length=1, max_length=40)
    valid_from: date
    valid_to: date | None = None
    expiry_values: dict[str, date] = Field(default_factory=dict)
    conditions: str | None = Field(default=None, max_length=4000)
    document_id: uuid.UUID | None = None


class OutcomeIn(_Loose):
    outcome: Literal["approved", "refused"]
    note: str | None = Field(default=None, max_length=4000)
    permit: PermitIn | None = None


class WithdrawIn(_Loose):
    reason: str = Field(min_length=1, max_length=2000)


class UnmaskOut(BaseModel):
    number: str


class StageOut(BaseModel):
    key: str
    label: str
    category: str
    sla_days: int | None = None


class CaseSummaryOut(BaseModel):
    id: uuid.UUID
    human_id: str
    candidate_id: uuid.UUID
    candidate_name: str | None
    visa_type_id: uuid.UUID
    visa_type_name: str
    visa_type_version_no: int | None
    country: str
    status: CaseStatus
    stage_key: str | None
    stage_label: str | None
    stage_category: str | None
    stage_entered_at: datetime | None
    owner_user_id: uuid.UUID
    owner_name: str | None
    receipt_number_last4: str | None
    next_key_date: date | None
    next_key_date_label: str | None
    overdue: bool
    created_at: datetime


class CaseOut(CaseSummaryOut):
    placement_id: uuid.UUID | None
    project_id: uuid.UUID | None
    demand_id: uuid.UUID | None
    business_unit_id: uuid.UUID | None
    counsel_id: uuid.UUID | None
    counsel_name: str | None
    sponsoring_legal_entity_id: uuid.UUID | None
    key_dates: dict[str, date]
    outcome_at: datetime | None
    outcome_note: str | None
    withdraw_reason: str | None
    lock_version: int
    stages: list[StageOut]
    key_date_defs: list[KeyDateDef]
    expiry_fields: list[ExpiryFieldDef]
    allowed_actions: list[str]
    disclaimer: str = DISCLAIMER


class CaseEventOut(BaseModel):
    id: uuid.UUID
    kind: str
    from_stage: str | None
    to_stage: str | None
    actor: dict[str, Any]
    payload: dict[str, Any]
    at: datetime


class DependantIn(_Loose):
    name: str = Field(min_length=1, max_length=200)
    relationship: Literal["spouse", "child", "parent", "other"]
    nationality: str | None = Field(default=None, min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")
    passport_number: str | None = Field(default=None, min_length=4, max_length=30)
    visa_status: Literal["not_started", "in_progress", "approved", "refused", "withdrawn"] = "not_started"
    notes: str | None = Field(default=None, max_length=4000)


class DependantPatch(_Loose):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    relationship: Literal["spouse", "child", "parent", "other"] | None = None
    nationality: str | None = Field(default=None, min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")
    passport_number: str | None = Field(default=None, min_length=4, max_length=30)
    visa_status: Literal["not_started", "in_progress", "approved", "refused", "withdrawn"] | None = None
    notes: str | None = Field(default=None, max_length=4000)


class DependantOut(BaseModel):
    id: uuid.UUID
    name: str
    relationship: str
    nationality: str | None
    passport_number_last4: str | None
    visa_status: str
    notes: str | None


# ================================================================ document requests
class RequestOut(BaseModel):
    id: uuid.UUID
    case_id: uuid.UUID
    checklist_key: str
    label: str
    document_category: str
    dependant_id: uuid.UUID | None
    dependant_name: str | None
    collect_from: str
    mandatory: bool
    status: RequestStatus
    document_id: uuid.UUID | None
    rejection_reason: str | None
    requested_at: datetime | None
    uploaded_at: datetime | None
    verified_at: datetime | None
    due_date: date | None
    warnings: list[str] = Field(default_factory=list)


class SendIn(_Loose):
    request_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    due_date: date | None = None


class SendOut(BaseModel):
    notified_worker: int
    worker_links: int
    counsel_links: int
    internal: int


class ReasonIn(_Loose):
    reason: str = Field(min_length=1, max_length=2000)


class VerifyOut(BaseModel):
    request: RequestOut
    warnings: list[str]


# ================================================================ permits
class PermitOut(BaseModel):
    id: uuid.UUID
    case_id: uuid.UUID
    candidate_id: uuid.UUID
    visa_type_id: uuid.UUID
    country: str
    permit_type: str
    number_last4: str | None
    valid_from: date
    valid_to: date | None
    expiry_values: dict[str, date]
    conditions: str | None
    document_id: uuid.UUID | None
    credential_id: uuid.UUID | None
    superseded_by_id: uuid.UUID | None
    status: Literal["active", "expired", "future", "superseded"]
    created_at: datetime


class PermitFacts(BaseModel):
    """``immigration_service.permits_for`` (public API): a Candidate's permits valid on a date. No numbers."""

    model_config = ConfigDict(frozen=True)

    permit_id: uuid.UUID
    case_id: uuid.UUID
    candidate_id: uuid.UUID
    country: str
    permit_type: str
    visa_type_key: str
    valid_from: date
    valid_to: date | None
    credential_id: uuid.UUID | None


# ================================================================ dashboard
class DashboardTile(BaseModel):
    key: str
    label: str
    count: int
    filters: dict[str, str]


class StageCount(BaseModel):
    category: str
    stage_key: str | None
    stage_label: str
    count: int


class CountryCount(BaseModel):
    country: str
    count: int


class DashboardOut(BaseModel):
    tiles: list[DashboardTile]
    by_stage: list[StageCount]
    by_country: list[CountryCount]


class AtRiskOut(BaseModel):
    placement_id: uuid.UUID
    placement_human_id: str
    candidate_id: uuid.UUID
    candidate_name: str | None
    work_country: str
    case_id: uuid.UUID | None
    case_human_id: str | None


# ================================================================ go-live import (FR-263, Epic 52)
class ImmigrationCaseImportIn(_Loose):
    """CSV columns of the go-live ``immigration_cases`` import. Permit columns are optional; when ``permit_number``
    is given the case is recorded Approved with that Permit (``recorded_by_user`` semantics: the importing user)."""

    candidate_id: uuid.UUID
    visa_type_key: str = Field(min_length=1, max_length=50)
    owner_user_id: uuid.UUID | None = None
    stage_key: str | None = Field(default=None, max_length=60)
    placement_id: uuid.UUID | None = None
    receipt_number: str | None = Field(default=None, max_length=40)
    permit_type: str | None = Field(default=None, max_length=200)
    permit_number: str | None = Field(default=None, max_length=40)
    permit_valid_from: date | None = None
    permit_valid_to: date | None = None
