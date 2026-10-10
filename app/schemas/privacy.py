"""Privacy API models (Epic 12: consent, AI-processing basis, DSRs, legal holds, erasure, retention)."""

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

ConsentPurpose = Literal[
    "data_processing", "ai_processing", "client_submission", "marketing",
    # Epic 35 (FR-179)
    "bgv", "reference_check", "vendor_collection", "whatsapp", "engagement",
]  # fmt: skip
DsrSubjectKind = Literal["candidate", "contact", "client_user", "vendor_user"]
AIProcessingBasis = Literal["consent_required", "notice_based"]
AIConsentState = Literal["granted", "pending", "withdrawn", "not_required"]
DsrKind = Literal["access", "correction", "erasure", "consent_withdrawal"]
DsrChannel = Literal["candidate_portal", "email", "phone", "letter", "other"]
DsrStatus = Literal["received", "in_progress", "completed", "refused"]
ConsentRequestStatus = Literal[
    "sent", "skipped_no_email", "skipped_already_granted", "skipped_recent", "not_found"
]
Text200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ basis / consents / notices
class AIBasisOut(BaseModel):
    basis: AIProcessingBasis
    pending_count: int = Field(description="Candidates currently flagged 'AI consent pending'")


class AIBasisUpdate(_In):
    basis: AIProcessingBasis


class ConsentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    candidate_id: uuid.UUID
    purpose: str
    client_id: uuid.UUID | None
    status: str
    source: str
    notice_id: uuid.UUID | None
    granted_at: datetime | None
    withdrawn_at: datetime | None
    withdrawn_by_kind: str | None


class ConsentRequestIn(_In):
    candidate_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    purpose: ConsentPurpose = "ai_processing"
    client_id: uuid.UUID | None = None


class ConsentRequestItemOut(BaseModel):
    candidate_id: uuid.UUID
    status: ConsentRequestStatus


class ConsentRequestResult(BaseModel):
    items: list[ConsentRequestItemOut]
    counts: dict[str, int]


class NoticeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    purpose: str
    locale: str
    version: int
    title: str
    body_md: str
    active: bool
    created_at: datetime


class NoticeCreate(_In):
    purpose: ConsentPurpose
    locale: str = Field(default="en", min_length=2, max_length=8)
    title: Text200
    body_md: str = Field(min_length=1, max_length=20000)


# ================================================================ DSRs
class DsrCreate(_In):
    candidate_id: uuid.UUID | None = None
    # Epic 35: contact / client_user / vendor_user subjects are internal intake only (``dsr:process``).
    subject_kind: DsrSubjectKind = "candidate"
    subject_id: uuid.UUID | None = None
    requester_name: Text200 | None = None
    requester_email: EmailStr | None = None
    kind: DsrKind
    channel: DsrChannel = "email"
    details: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime | None = None


class DsrUpdate(_In):
    candidate_id: uuid.UUID | None = None
    assigned_to: uuid.UUID | None = None
    details: dict[str, Any] | None = None


class DsrTransitionIn(_In):
    to: DsrStatus
    refusal_reason: str | None = Field(default=None, max_length=4000)
    note: str | None = Field(default=None, max_length=4000)


class DsrExecuteIn(_In):
    # consent_withdrawal: the purposes to withdraw (default: all granted purposes).
    purposes: list[ConsentPurpose] | None = None
    confirm: str | None = Field(default=None, description="Erasure requires the typed confirmation 'ERASE'")


class ErasureRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    candidate_id: uuid.UUID
    reason: str
    dsr_id: uuid.UUID | None
    retention_run_id: uuid.UUID | None
    status: str
    handler_results: dict[str, Any]
    verification: dict[str, Any] | None
    retained_summary: list[dict[str, Any]] | None = None
    started_at: datetime | None
    finished_at: datetime | None


class DsrOut(BaseModel):
    id: uuid.UUID
    human_id: str
    candidate_id: uuid.UUID | None
    subject_kind: DsrSubjectKind = "candidate"
    subject_id: uuid.UUID | None = None
    candidate_human_id: str | None = None
    requester_name: str | None
    requester_email: str | None
    kind: DsrKind
    channel: DsrChannel
    details: dict[str, Any]
    status: DsrStatus
    received_at: datetime
    due_at: datetime
    overdue: bool
    assigned_to: uuid.UUID | None
    identity_verified_at: datetime | None
    identity_verified_by: uuid.UUID | None
    refusal_reason: str | None
    completed_at: datetime | None
    completed_by: uuid.UUID | None
    result: dict[str, Any] | None
    created_at: datetime


class TimelineEntry(BaseModel):
    occurred_at: datetime
    action: str
    actor_type: str
    actor_id: uuid.UUID | None
    data: dict[str, Any] | None


class DsrDetailOut(DsrOut):
    timeline: list[TimelineEntry]
    erasure_run: ErasureRunOut | None = None
    legal_hold: "LegalHoldOut | None" = None


class DsrFilters(BaseModel):
    status: DsrStatus | None = None
    kind: DsrKind | None = None
    overdue: bool | None = None
    assigned_to: uuid.UUID | None = None


# ================================================================ legal holds
class LegalHoldCreate(_In):
    candidate_id: uuid.UUID
    reason: str = Field(min_length=3, max_length=2000)


class LegalHoldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    candidate_id: uuid.UUID
    reason: str
    placed_by: uuid.UUID | None
    placed_at: datetime
    released_by: uuid.UUID | None
    released_at: datetime | None


# ================================================================ retention
class RetentionPolicyOut(BaseModel):
    candidate_inactive_months: int | None
    auto_execute: bool
    audit_retention_years: int


class RetentionPolicyUpdate(_In):
    candidate_inactive_months: int | None = Field(default=None, ge=6, le=120)
    auto_execute: bool | None = None
    audit_retention_years: int | None = Field(default=None, ge=1, le=10)


class RetentionPreviewIn(_In):
    # Defaults to the policy's value; required when the policy has none.
    candidate_inactive_months: int | None = Field(default=None, ge=6, le=120)


class RetentionRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str = "inactive_candidates"
    status: str
    criteria: dict[str, Any]
    candidate_count: int
    created_by: uuid.UUID | None
    created_at: datetime
    executed_by: uuid.UUID | None
    executed_at: datetime | None
    auto_execute_after: datetime | None
    counts: dict[str, int] = Field(default_factory=dict)


class RetentionItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    candidate_id: uuid.UUID
    last_activity_at: datetime | None
    status: str


class RetentionExecuteIn(_In):
    # ``ANONYMIZE`` for inactive-candidate runs, ``EXPIRE`` for retention-class expiry runs (Epic 35).
    confirm: Literal["ANONYMIZE", "EXPIRE"]


# ================================================================ public secure links
class PublicLinkOut(BaseModel):
    purpose: str
    tenant_name: str | None = None
    expires_at: datetime
    notice: NoticeOut | None = None
    download_url: str | None = None


class PublicConsentIn(_In):
    granted: bool
    notice_id: uuid.UUID | None = None


class PublicConsentOut(BaseModel):
    status: Literal["granted", "declined"]


DsrDetailOut.model_rebuild()


# ================================================================ retention classes (Epic 35, AD-34)
RetentionAnchor = Literal["document_date", "fy_end", "adjudicated_at", "signed_at", "placement_end", "period_end"]
RetentionExpiryAction = Literal["anonymize", "delete", "report_only"]
ClassKey = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_]{1,39}$")]


class RetentionClassOut(BaseModel):
    id: uuid.UUID
    key: str
    label: str
    period_months: int
    anchor: RetentionAnchor
    expiry_action: RetentionExpiryAction
    is_system: bool
    floor_months: int
    auto_execute: bool
    archived_at: datetime | None


class RetentionClassCreate(_In):
    key: ClassKey
    label: Text200
    period_months: int = Field(ge=1, le=1200)
    anchor: RetentionAnchor = "document_date"
    expiry_action: RetentionExpiryAction = "anonymize"
    auto_execute: bool = False


class RetentionClassUpdate(_In):
    label: Text200 | None = None
    period_months: int | None = Field(default=None, ge=0, le=1200)
    expiry_action: RetentionExpiryAction | None = None
    auto_execute: bool | None = None


class RetentionBindingOut(BaseModel):
    table: str
    class_key: str
    status: Literal["registered", "pending"]
    owner_epic: str | None = None


class ClassExpiryTableOut(BaseModel):
    table: str
    count: int
    skipped_legal_hold: int = 0
    skipped_override: int = 0


class ClassExpiryPreviewOut(BaseModel):
    run_id: uuid.UUID | None
    class_key: str
    report_only: bool
    reason: str | None = None
    total: int
    by_table: list[ClassExpiryTableOut]
    valid_until: datetime | None = None


class RetentionOverrideIn(_In):
    subject_kind: DsrSubjectKind = "candidate"
    subject_id: uuid.UUID
    class_key: ClassKey
    until_date: date
    reason: str | None = Field(default=None, max_length=2000)
