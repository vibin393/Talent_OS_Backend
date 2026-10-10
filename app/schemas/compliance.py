"""Compliance DTOs: the Epic 13 contract (``WorkerComplianceStatus``, frozen) plus the Epic 21 API schemas."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WorkerComplianceStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: uuid.UUID
    compliant: bool
    expired_mandatory: list[str]


# ================================================================ Epic 21
CredentialCategory = Literal[
    "passport",
    "visa_work_permit",
    "certification",
    "professional_licence",
    "insurance",
    "client_document",
    "identity",
    "other",
]
NumberKind = Literal["none", "encrypted", "aadhaar_last4"]
ItemKind = Literal["credential", "document", "bgv_package", "policy_ack"]


class CredentialTypeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=2, max_length=60, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=200)
    category: CredentialCategory
    has_expiry: bool = True
    number_kind: NumberKind = "none"
    requires_document: bool = True
    alert_offsets: list[int] | None = None

    @field_validator("alert_offsets")
    @classmethod
    def _offsets(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and any(o < 0 or o > 365 for o in v):
            raise ValueError("offsets must be between 0 and 365 days")
        return sorted(set(v), reverse=True) if v is not None else None


class CredentialTypeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=200)
    has_expiry: bool | None = None
    requires_document: bool | None = None
    alert_offsets: list[int] | None = None
    archived: bool | None = None


class CredentialTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    label: str
    category: str
    has_expiry: bool
    number_kind: str
    requires_document: bool
    alert_offsets: list[int] | None
    archived_at: datetime | None


class RequirementMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_keywords: list[str] = []
    industries: list[str] = []
    countries: list[str] = []
    worker_types: list[str] = []
    engagement_kinds: list[str] = []


class RequirementItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ItemKind
    ref: str = Field(min_length=1, max_length=100)
    mandatory: bool = True
    label: str = Field(min_length=1, max_length=200)


class RequirementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    client_id: uuid.UUID | None = None
    match: RequirementMatch = RequirementMatch()
    items: list[RequirementItem] = Field(min_length=1)
    active: bool = True
    effective_from: date | None = None

    @field_validator("items")
    @classmethod
    def _unique(cls, v: list[RequirementItem]) -> list[RequirementItem]:
        keys = [(i.kind, i.ref) for i in v]
        if len(keys) != len(set(keys)):
            raise ValueError("an item (kind, ref) may appear only once")
        return v


class RequirementUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    match: RequirementMatch | None = None
    items: list[RequirementItem] | None = Field(default=None, min_length=1)
    active: bool | None = None
    effective_from: date | None = None
    archived: bool | None = None


class RequirementItemOut(RequirementItem):
    unresolved: bool = False


class RequirementOut(BaseModel):
    id: uuid.UUID
    name: str
    client_id: uuid.UUID | None
    match: RequirementMatch
    items: list[RequirementItemOut]
    active: bool
    effective_from: date | None
    archived_at: datetime | None
    created_at: datetime
    evaluation_run_id: uuid.UUID | None = None


class ComplianceSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    evaluate_at: Literal["placement_created", "application_selected"]
    default_alert_offsets: list[int]
    recompute_hour_local: int
    timezone: str


class ComplianceSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluate_at: Literal["placement_created", "application_selected"] | None = None
    default_alert_offsets: list[int] | None = Field(default=None, min_length=1)
    recompute_hour_local: int | None = Field(default=None, ge=0, le=23)


class EvaluationRunOut(BaseModel):
    id: uuid.UUID
    requirement_id: uuid.UUID | None
    started_at: datetime
    finished_at: datetime | None
    placements_evaluated: int
    newly_non_compliant: list[uuid.UUID]
    placements: list[dict[str, object]] = []


class CredentialIn(BaseModel):
    """Record a credential (multipart form fields; the file travels separately)."""

    model_config = ConfigDict(extra="forbid")

    type_key: str = Field(min_length=1, max_length=60)
    number: str | None = Field(default=None, max_length=60)
    issuer: str | None = Field(default=None, max_length=200)
    issued_on: date | None = None
    expires_on: date | None = None


class RenewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str | None = Field(default=None, max_length=60)
    issuer: str | None = Field(default=None, max_length=200)
    issued_on: date | None = None
    expires_on: date | None = None


class VerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["valid", "rejected"]
    note: str | None = Field(default=None, max_length=2000)


class RedactionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["confirmed", "rejected"]


class CredentialOut(BaseModel):
    id: uuid.UUID
    human_id: str
    candidate_id: uuid.UUID
    candidate_name: str | None = None
    credential_type_id: uuid.UUID
    type_key: str
    type_label: str | None = None
    number_kind: str | None = None
    number_last4: str | None
    issuer: str | None
    issued_on: date | None
    expires_on: date | None
    document_id: uuid.UUID | None
    redaction_status: str | None
    verification_status: str
    expiry_status: str
    status: str  # effective: pending_verification | valid | expiring_soon | expired | rejected | superseded
    version_no: int
    previous_id: uuid.UUID | None
    superseded_at: datetime | None
    verified_by: uuid.UUID | None
    verified_at: datetime | None
    rejection_note: str | None
    source: str
    created_at: datetime


class UnmaskOut(BaseModel):
    number: str


class ChecklistItemOut(BaseModel):
    id: uuid.UUID
    kind: str
    ref: str
    label: str
    mandatory: bool
    status: str
    retroactive: bool
    credential_id: uuid.UUID | None
    waived_reason: str | None
    waived_by: uuid.UUID | None
    waived_at: datetime | None
    requirement_names: list[str]
    unresolved: bool = False


class PlacementComplianceOut(BaseModel):
    placement_id: uuid.UUID
    non_compliant: bool
    blocking: bool
    items: list[ChecklistItemOut]


class WaiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=10, max_length=2000)

    @field_validator("reason")
    @classmethod
    def _r(cls, v: str) -> str:
        if len(v.strip()) < 10:
            raise ValueError("give a reason of at least 10 characters")
        return v.strip()


class DashboardPlacementRow(BaseModel):
    placement_id: uuid.UUID
    human_id: str
    candidate_name: str | None
    client_name: str | None
    status: str
    blocking: bool
    missing: int
    expired: int


class DashboardCredentialRow(BaseModel):
    credential_id: uuid.UUID
    human_id: str
    candidate_id: uuid.UUID
    candidate_name: str | None
    type_label: str
    number_last4: str | None
    expires_on: date | None
    days_left: int | None


class ComplianceDashboardOut(BaseModel):
    compliant: int
    non_compliant: int
    blocking: int
    retroactive_only: int
    expiring_30: int
    expiring_60: int
    missing_by_kind: dict[str, int]
    top_non_compliant: list[DashboardPlacementRow]
    expiring: list[DashboardCredentialRow]
