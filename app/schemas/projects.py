# ruff: noqa: E501
"""Project / SOW DTOs (Epic 45; Epic 36 contract, AD-39).

The first block (``ProjectRoleRef``, ``ProjectContext``) is the frozen contract layer: changing a field needs a
spine amendment. The Epic 45 API schemas follow it (internal ``/api/v1/projects``); the Client Portal views live
in ``app/schemas/projects_portal.py``. Money is ``{"amount", "currency"}`` with decimal strings (AD-20).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.money import DecimalStr, MoneyOut
from app.schemas.commercials import MilestoneOut

__all__ = [
    "AssignmentEndIn",
    "AssignmentIn",
    "AssignmentOut",
    "BudgetIn",
    "BudgetOut",
    "BudgetsIn",
    "BurnOut",
    "DeliverableEventOut",
    "DeliverableIn",
    "DeliverableOut",
    "DeliverableRejectIn",
    "DeliverableUpdate",
    "DemandFromRoleIn",
    "Project360Out",
    "ProjectAssignmentImportIn",
    "ProjectContext",
    "ProjectCreate",
    "ProjectImportIn",
    "ProjectInvoiceRef",
    "ProjectListItem",
    "ProjectOut",
    "ProjectRisk",
    "ProjectRoleImportIn",
    "ProjectRoleIn",
    "ProjectRoleOut",
    "ProjectRoleRef",
    "ProjectRoleUpdate",
    "ProjectTransitionIn",
    "ProjectUpdate",
    "RoleSkill",
    "SowSummary",
    "TimelineOut",
]


# ================================================================ frozen contract (Epic 36)
class ProjectRoleRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    title: str
    headcount: int
    status: str
    start_date: date | None
    end_date: date | None
    filled_count: int


class ProjectContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_id: uuid.UUID
    human_id: str
    name: str
    client_id: uuid.UUID
    contract_id: uuid.UUID | None
    status: str
    start_date: date | None
    end_date: date | None
    roles: tuple[ProjectRoleRef, ...] = ()


# ================================================================ Epic 45 API
Str200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=10000)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=2000)]
ProjectStatus = Literal["draft", "active", "on_hold", "completed", "cancelled"]
RoleStatus = Literal["open", "filled", "closed", "cancelled"]
RateUnit = Literal["hour", "day", "month", "fixed"]
Dimension = Literal["hours", "cost", "revenue"]
DeliverableStatus = Literal["planned", "in_progress", "submitted", "accepted", "rejected", "cancelled"]
Pct = Annotated[Decimal, Field(gt=Decimal(0), le=Decimal(100))]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(_In):
    name: Str200
    client_id: uuid.UUID
    contract_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    business_unit_id: uuid.UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    description: Text | None = None
    risk_note: Text | None = None


class ProjectUpdate(_In):
    name: Str200 | None = None
    contract_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    business_unit_id: uuid.UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    description: Text | None = None
    risk_note: Text | None = None


class ProjectTransitionIn(_In):
    to: ProjectStatus


class ProjectOut(BaseModel):
    id: uuid.UUID
    human_id: str
    name: str
    client_id: uuid.UUID
    client_name: str | None = None
    contract_id: uuid.UUID | None
    contract_human_id: str | None = None
    owner_id: uuid.UUID | None
    owner_name: str | None = None
    business_unit_id: uuid.UUID | None
    legal_entity_id: uuid.UUID | None
    currency: str | None
    status: ProjectStatus
    start_date: date | None
    end_date: date | None
    description: str | None
    risk_note: str | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ProjectListItem(ProjectOut):
    burn_pct: DecimalStr | None = None
    risks_count: int = 0


class RoleSkill(BaseModel):
    """A role skill: ``skill_key`` is ``SkillKey.as_str()`` (``global:<uuid>`` / ``tenant:<uuid>``)."""

    model_config = ConfigDict(extra="forbid")

    skill_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=60)]
    label: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    min_proficiency: Annotated[int, Field(ge=1, le=5)] | None = None


class ProjectRoleIn(_In):
    title: Str200
    headcount: Annotated[int, Field(ge=1, le=10000)] = 1
    start_date: date | None = None
    end_date: date | None = None
    skills: list[RoleSkill] = Field(default_factory=list, max_length=50)
    bill_rate: DecimalStr | None = Field(default=None, ge=Decimal(0))
    bill_rate_unit: RateUnit | None = None
    notes: Text | None = None


class ProjectRoleUpdate(_In):
    title: Str200 | None = None
    headcount: Annotated[int, Field(ge=1, le=10000)] | None = None
    start_date: date | None = None
    end_date: date | None = None
    skills: list[RoleSkill] | None = Field(default=None, max_length=50)
    bill_rate: DecimalStr | None = Field(default=None, ge=Decimal(0))
    bill_rate_unit: RateUnit | None = None
    notes: Text | None = None
    # A User closes / cancels a role or reopens it; ``filled`` is derived from the assignments.
    status: Literal["open", "closed", "cancelled"] | None = None


class ProjectRoleOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    headcount: int
    status: RoleStatus
    start_date: date | None
    end_date: date | None
    skills: list[RoleSkill]
    bill_rate: DecimalStr | None
    bill_rate_unit: str | None
    notes: str | None
    filled_fte: DecimalStr
    active_assignments: int
    demand_ids: list[uuid.UUID] = Field(default_factory=list)


class DemandFromRoleIn(_In):
    positions: Annotated[int, Field(ge=1, le=10000)] | None = None


class AssignmentIn(_In):
    placement_id: uuid.UUID
    allocation_pct: Pct = Decimal(100)
    start_date: date
    end_date: date | None = None


class AssignmentEndIn(_In):
    end_date: date
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] | None = (
        None
    )


class AssignmentOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    project_role_id: uuid.UUID
    role_title: str | None = None
    placement_id: uuid.UUID
    placement_human_id: str | None = None
    candidate_id: uuid.UUID | None
    worker_name: str | None = None
    allocation_pct: DecimalStr
    start_date: date
    end_date: date | None
    status: Literal["active", "ended"]
    ended_reason: str | None
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- timeline
class RoleSegment(BaseModel):
    start: date
    end: date
    assigned_fte: DecimalStr
    headcount: int
    gap: bool


class RoleTimeline(BaseModel):
    project_role_id: uuid.UUID
    title: str
    headcount: int
    segments: list[RoleSegment]


class AllocationSplit(BaseModel):
    project_id: uuid.UUID
    project_name: str
    allocation_pct: DecimalStr


class WorkerSegment(BaseModel):
    start: date
    end: date
    total_pct: DecimalStr
    over: bool
    split: list[AllocationSplit]


class WorkerTimeline(BaseModel):
    candidate_id: uuid.UUID | None
    placement_id: uuid.UUID
    worker_name: str | None
    segments: list[WorkerSegment]


class TimelineOut(BaseModel):
    from_date: date
    to_date: date
    roles: list[RoleTimeline]
    workers: list[WorkerTimeline]
    gap_count: int
    over_allocation_count: int


# ---------------------------------------------------------------- deliverables
class DeliverableIn(_In):
    name: Str200
    description: Text | None = None
    acceptance_criteria: Text | None = None
    due_date: date | None = None
    milestone_id: uuid.UUID | None = None


class DeliverableUpdate(_In):
    name: Str200 | None = None
    description: Text | None = None
    acceptance_criteria: Text | None = None
    due_date: date | None = None
    milestone_id: uuid.UUID | None = None
    lock_version: int | None = None


class DeliverableRejectIn(_In):
    reason: Reason


class DeliverableEventOut(BaseModel):
    id: uuid.UUID
    kind: str
    actor: dict[str, Any]
    reason: str | None
    evidence_file_id: uuid.UUID | None
    at: datetime


class DeliverableOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    milestone_id: uuid.UUID | None
    milestone_name: str | None = None
    name: str
    description: str | None
    acceptance_criteria: str | None
    due_date: date | None
    status: DeliverableStatus
    submitted_at: datetime | None
    accepted_at: datetime | None
    accepted_actor: dict[str, Any] | None
    last_rejection_reason: str | None
    evidence_file_ids: list[uuid.UUID]
    lock_version: int
    events: list[DeliverableEventOut] = Field(default_factory=list)


# ---------------------------------------------------------------- budgets / burn
class BudgetIn(_In):
    dimension: Dimension
    amount: DecimalStr = Field(ge=Decimal(0))


class BudgetsIn(_In):
    budgets: list[BudgetIn] = Field(min_length=1, max_length=3)


class BudgetOut(BaseModel):
    dimension: Dimension
    amount: DecimalStr
    currency: str | None
    version: int
    alert_80_at: datetime | None
    alert_100_at: datetime | None


class BurnOut(BaseModel):
    dimension: Dimension
    actual: DecimalStr
    budget: DecimalStr | None
    pct: DecimalStr | None
    burn_rate_per_day: DecimalStr
    forecast_at_completion: DecimalStr
    currency: str | None
    as_of: date
    breakdown: dict[str, Any]


# ---------------------------------------------------------------- Project 360
class SowSummary(BaseModel):
    contract_id: uuid.UUID
    human_id: str
    title: str | None
    pricing_model: str | None
    total_value: MoneyOut | None
    status: str
    start_date: date
    end_date: date | None


class ProjectInvoiceRef(BaseModel):
    kind: Literal["invoice", "credit_note"]
    id: uuid.UUID
    number: str | None
    doc_date: date | None
    status: str
    amount: MoneyOut


class ProjectRisk(BaseModel):
    kind: Literal["deliverable_overdue", "role_unfilled", "over_allocation", "budget_threshold", "sow_ending"]
    message: str
    ref_id: uuid.UUID | None = None


class Project360Out(BaseModel):
    project: ProjectOut
    sow: SowSummary | None
    roles: list[ProjectRoleOut]
    assignments: list[AssignmentOut]
    gap_count: int
    over_allocation_count: int
    milestones: list[MilestoneOut]
    deliverables: list[DeliverableOut]
    budgets: list[BudgetOut]
    burn: list[BurnOut]
    invoices: list[ProjectInvoiceRef]
    risks: list[ProjectRisk]


# ---------------------------------------------------------------- go-live imports (FR-263; names = CSV columns)
class ProjectImportIn(_In):
    name: Str200
    client_name: Str200
    sow_human_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] | None = None
    owner_email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=320)] | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: Literal["draft", "active", "on_hold", "completed"] = "draft"
    description: Text | None = None


class ProjectRoleImportIn(_In):
    project_human_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
    title: Str200
    headcount: Annotated[int, Field(ge=1, le=10000)] = 1
    start_date: date | None = None
    end_date: date | None = None


class ProjectAssignmentImportIn(_In):
    project_human_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
    role_title: Str200
    placement_human_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
    allocation_pct: Pct = Decimal(100)
    start_date: date
    end_date: date | None = None
