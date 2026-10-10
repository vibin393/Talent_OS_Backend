"""Digital onboarding internal API schemas (Epic 23)."""

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

TaskKind = Literal[
    "personal_details",
    "bank_details",
    "tax_details",
    "emergency_contact",
    "document_upload",
    "esign",
    "policy_ack",
    "client_form",
    "credential",
    "internal",
]
Assignee = Literal["worker", "user"]
OwnerKind = Literal["role", "placement_account_manager", "placement_recruiter", "case_owner"]
CaseStatus = Literal["not_started", "in_progress", "complete", "cancelled"]
TaskStatus = Literal["pending", "submitted", "returned", "approved", "waived"]
_Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
_Key = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100, pattern=r"^[a-z0-9_:\-]+$")
]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- settings
class OnboardingSettingsOut(_Out):
    auto_invite: bool
    worker_reminder_every_days: int
    default_owner_by_role: dict[str, uuid.UUID | None]
    default_hr_owner_id: uuid.UUID | None
    default_tenant_signatory_user_id: uuid.UUID | None
    reuse_validity_days: int


class OnboardingSettingsUpdate(_In):
    auto_invite: bool | None = None
    worker_reminder_every_days: Annotated[int, Field(ge=1, le=30)] | None = None
    default_owner_by_role: dict[str, uuid.UUID | None] | None = None
    default_hr_owner_id: uuid.UUID | None = None
    default_tenant_signatory_user_id: uuid.UUID | None = None
    reuse_validity_days: Annotated[int, Field(ge=1, le=3650)] | None = None


# ---------------------------------------------------------------- templates
class TemplateTaskIn(_In):
    key: _Key
    kind: TaskKind
    label: _Name
    instructions: Annotated[str, StringConstraints(max_length=4000)] | None = None
    mandatory: bool = True
    assignee: Assignee | None = None  # default: user for internal, worker otherwise
    owner_kind: OwnerKind | None = None
    owner_role_key: Annotated[str, StringConstraints(max_length=60)] | None = None
    due_offset_days: Annotated[int, Field(ge=-365, le=365)] = 0
    auto_approve: bool = False
    reusable: bool = False
    validity_days: Annotated[int, Field(ge=1, le=3650)] | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    position: int | None = None


class TemplateTaskOut(_Out):
    id: uuid.UUID
    key: str
    position: int
    kind: str
    label: str
    instructions: str | None
    mandatory: bool
    assignee: str
    owner_kind: str | None
    owner_role_key: str | None
    due_offset_days: int
    auto_approve: bool
    reusable: bool
    validity_days: int | None
    config: dict[str, Any]


class TemplateIn(_In):
    name: _Name
    legal_entity_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    worker_type: Annotated[str, StringConstraints(max_length=20)] | None = None
    country: Annotated[str, StringConstraints(to_upper=True, min_length=2, max_length=2)] | None = None
    active: bool = True
    tasks: list[TemplateTaskIn] = Field(default_factory=list, max_length=60)


class TemplatePatch(_In):
    name: _Name | None = None
    legal_entity_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    worker_type: Annotated[str, StringConstraints(max_length=20)] | None = None
    country: Annotated[str, StringConstraints(to_upper=True, min_length=2, max_length=2)] | None = None
    active: bool | None = None
    archived: bool | None = None


class TemplateTasksPut(_In):
    tasks: list[TemplateTaskIn] = Field(max_length=60)


class TemplateOut(_Out):
    id: uuid.UUID
    name: str
    legal_entity_id: uuid.UUID | None
    client_id: uuid.UUID | None
    worker_type: str | None
    country: str | None
    active: bool
    archived_at: datetime | None
    version: int
    tasks: list[TemplateTaskOut] = Field(default_factory=list)


class TemplateMatchOut(BaseModel):
    placement_id: uuid.UUID
    template_id: uuid.UUID | None
    template_name: str | None


# ---------------------------------------------------------------- cases / tasks
class TaskOut(_Out):
    id: uuid.UUID
    case_id: uuid.UUID
    key: str
    position: int
    kind: str
    label: str
    instructions: str | None
    mandatory: bool
    assignee: str
    owner_user_id: uuid.UUID | None
    p1_task_id: uuid.UUID | None
    due_on: date | None
    source: str
    status: str
    submitted_at: datetime | None
    auto_approve: bool
    config: dict[str, Any]
    data: dict[str, Any] | None
    document_ids: list[uuid.UUID]
    signature_request_id: uuid.UUID | None
    credential_id: uuid.UUID | None
    return_comment: str | None
    waived_reason: str | None
    decided_by_kind: str | None
    decided_by_id: uuid.UUID | None
    decided_at: datetime | None
    reused_from_task_id: uuid.UUID | None
    overdue: bool = False


class CaseOut(_Out):
    id: uuid.UUID
    human_id: str | None
    placement_id: uuid.UUID
    placement_human_id: str | None = None
    candidate_id: uuid.UUID
    worker_name: str | None = None
    client_id: uuid.UUID | None = None
    client_name: str | None = None
    planned_start_date: date | None = None
    template_id: uuid.UUID | None
    template_name: str | None = None
    status: str
    owner_user_id: uuid.UUID | None
    invited_at: datetime | None
    progress_pct: int
    completed_at: datetime | None
    cancelled_reason: str | None
    gate_status: str | None = None
    bank_last4: str | None = None
    pan_last4: str | None = None
    tasks: list[TaskOut] = Field(default_factory=list)


class BoardRow(BaseModel):
    id: uuid.UUID
    human_id: str | None
    placement_id: uuid.UUID
    placement_human_id: str | None
    candidate_id: uuid.UUID
    worker_name: str | None
    client_id: uuid.UUID | None
    client_name: str | None
    planned_start_date: date | None
    status: str
    owner_user_id: uuid.UUID | None
    progress_pct: int
    counts: dict[str, int]
    overdue: int
    oldest_submitted_days: int | None


class UnmatchedPlacement(BaseModel):
    placement_id: uuid.UUID
    human_id: str
    candidate_id: uuid.UUID
    client_id: uuid.UUID
    planned_start_date: date


class ApproveIn(_In):
    pan_name_checked: bool | None = None
    note: Annotated[str, StringConstraints(max_length=1000)] | None = None


class ReturnIn(_In):
    comment: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=2000)]


class WaiveIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=2000)]


class BulkApproveIn(_In):
    task_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class BulkResultOut(BaseModel):
    approved: list[uuid.UUID]
    failed: dict[str, str]


class InviteOut(BaseModel):
    invited_at: datetime


class CaseCreateIn(_In):
    placement_id: uuid.UUID
