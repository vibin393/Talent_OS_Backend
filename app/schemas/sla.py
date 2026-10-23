"""Generic SLA clocks (Epic 13, AD-31)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.core.money import DecimalStr


class SlaClockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    milestone: str
    ref_id: uuid.UUID | None
    target_id: uuid.UUID | None
    target_hours: DecimalStr
    calendar: str
    at_risk_percent: int
    started_at: datetime
    due_at: datetime
    at_risk_at: datetime
    completed_at: datetime | None
    outcome: str | None


# ================================================================ Epic 32: targets, clocks, escalations
from decimal import Decimal  # ruff: ignore[module-import-not-at-top-of-file]
from typing import Annotated, Any, Literal  # ruff: ignore[module-import-not-at-top-of-file]

from pydantic import (  # ruff: ignore[module-import-not-at-top-of-file]
    Field,
    StringConstraints,
    model_validator,
)

ClockStatus = Literal["healthy", "at_risk", "breached", "met", "cancelled"]
Calendar = Literal["business", "calendar"]
AfterUnit = Literal["business_hours", "business_days", "hours"]
TargetKind = Literal["named_user", "assignee_manager", "role"]
Priority = Literal["low", "medium", "high", "critical"]


class SlaMilestoneOut(BaseModel):
    subject_type: str
    milestone: str
    label: str
    scope_keys: list[str]
    calendar: str
    owner_epic: str | None
    available: bool


class GenericSlaTargetIn(BaseModel):
    subject_type: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    milestone: Annotated[str, StringConstraints(min_length=1, max_length=30)]
    scope: dict[str, str] = Field(default_factory=dict)
    priority: Priority | None = None
    target_hours: Annotated[Decimal, Field(gt=0, le=8760, max_digits=8, decimal_places=2)]
    at_risk_percent: Annotated[int, Field(ge=1, le=99)] = 75
    calendar: Calendar = "business"
    is_active: bool = True


class GenericSlaTargetUpdate(BaseModel):
    scope: dict[str, str] | None = None
    priority: Priority | None = None
    target_hours: Annotated[Decimal, Field(gt=0, le=8760, max_digits=8, decimal_places=2)] | None = None
    at_risk_percent: Annotated[int, Field(ge=1, le=99)] | None = None
    calendar: Calendar | None = None
    is_active: bool | None = None


class GenericSlaTargetOut(BaseModel):
    id: uuid.UUID
    subject_type: str
    milestone: str
    scope: dict[str, Any]
    priority: str | None
    target_hours: DecimalStr
    at_risk_percent: int
    calendar: str
    is_active: bool
    available: bool
    created_at: datetime
    updated_at: datetime


class SlaClockView(BaseModel):
    """A clock as the API shows it: ``status`` is the evaluator status, kept after completion (a breach is
    never erased)."""

    id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    milestone: str
    ref_id: uuid.UUID | None
    target_hours: DecimalStr
    calendar: str
    at_risk_percent: int
    started_at: datetime
    due_at: datetime
    at_risk_at: datetime
    completed_at: datetime | None
    breached_at: datetime | None
    status: ClockStatus


class SlaSummaryOut(BaseModel):
    subject_type: str
    milestone: str | None
    counts: dict[str, int]
    total: int


class EscalationPolicyIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    after_value: Annotated[Decimal, Field(gt=0, le=2000, max_digits=8, decimal_places=2)]
    after_unit: AfterUnit = "business_hours"
    action: Literal["notify", "reassign"] = "notify"
    target_kind: TargetKind
    target_user_id: uuid.UUID | None = None
    target_role_id: uuid.UUID | None = None
    repeat_every_value: Annotated[Decimal, Field(gt=0, le=2000, max_digits=8, decimal_places=2)] | None = None
    max_repeats: Annotated[int, Field(ge=0, le=20)] = 0
    notify_original_assignee: bool = True
    is_active: bool = True

    @model_validator(mode="after")
    def _target(self) -> "EscalationPolicyIn":
        if self.target_kind == "named_user" and (self.target_user_id is None or self.target_role_id is not None):
            msg = "named_user needs target_user_id only"
            raise ValueError(msg)
        if self.target_kind == "role" and (self.target_role_id is None or self.target_user_id is not None):
            msg = "role needs target_role_id only"
            raise ValueError(msg)
        if self.target_kind == "assignee_manager" and (self.target_user_id or self.target_role_id):
            msg = "assignee_manager takes no target id"
            raise ValueError(msg)
        if self.max_repeats > 0 and self.repeat_every_value is None:
            msg = "repeats need repeat_every_value"
            raise ValueError(msg)
        return self


class EscalationPolicyUpdate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    after_value: Annotated[Decimal, Field(gt=0, le=2000, max_digits=8, decimal_places=2)] | None = None
    after_unit: AfterUnit | None = None
    action: Literal["notify", "reassign"] | None = None
    target_kind: TargetKind | None = None
    target_user_id: uuid.UUID | None = None
    target_role_id: uuid.UUID | None = None
    repeat_every_value: Annotated[Decimal, Field(gt=0, le=2000, max_digits=8, decimal_places=2)] | None = None
    max_repeats: Annotated[int, Field(ge=0, le=20)] | None = None
    notify_original_assignee: bool | None = None
    is_active: bool | None = None


class EscalationPolicyOut(BaseModel):
    id: uuid.UUID
    name: str | None
    owner_kind: str
    owner_ref: str | None
    after_value: DecimalStr
    after_unit: str
    after_business_hours: DecimalStr
    calendar: str
    action: str
    target_kind: TargetKind
    target_user_id: uuid.UUID | None
    target_role_id: uuid.UUID | None
    repeat_every_value: DecimalStr | None
    max_repeats: int
    notify_original_assignee: bool
    is_active: bool
    archived_at: datetime | None
    source_policy_id: uuid.UUID | None
    summary: str
    created_at: datetime
    updated_at: datetime


class ChainStepEscalationIn(BaseModel):
    position: Annotated[int, Field(ge=1, le=50)]
    policy_id: uuid.UUID | None = None


class ChainEscalationsIn(BaseModel):
    steps: Annotated[list[ChainStepEscalationIn], Field(max_length=50)]


class ChainStepEscalationOut(BaseModel):
    position: int
    policy_id: uuid.UUID | None  # the library policy the step's copy was made from
    copy_id: uuid.UUID
    name: str | None
    summary: str


class ChainEscalationsOut(BaseModel):
    chain_id: uuid.UUID
    steps: list[ChainStepEscalationOut]


class EscalationWatchOut(BaseModel):
    id: uuid.UUID
    policy_id: uuid.UUID | None
    subject_kind: str
    subject_id: uuid.UUID
    step_position: int | None
    started_at: datetime
    next_fire_at: datetime | None
    fires: int
    status: str
    last_outcome: str | None
    history: list[dict[str, Any]]
    spec: dict[str, Any]
