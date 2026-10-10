"""Approval chains, requests and decisions (story 4.2)."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

SubjectType = Literal["demand", "offer", "margin_override", "contract"]  # Epic 16: contract approval chains
RequestStatus = Literal["pending", "approved", "rejected", "changes_requested", "cancelled"]
DecisionOutcome = Literal["approved", "rejected", "changes_requested", "skipped"]
DecideAction = Literal["approve", "reject", "request_changes"]

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChainStepIn(_In):
    name: Name
    approver_user_id: uuid.UUID | None = None
    approver_role_id: uuid.UUID | None = None
    # Epic 13 (R5): steps of other kinds (e.g. a client approver) resolve through the registered resolver.
    approver_kind: Annotated[str, StringConstraints(min_length=1, max_length=32)] | None = None
    approver_ref: dict[str, Any] | None = None
    condition: dict[str, Any] | None = Field(
        default=None, description='{"all"|"any": [{"field", "op": eq|ne|gt|gte|lt|lte|in, "value"}]}'
    )


class ChainIn(_In):
    subject_type: SubjectType
    name: Name
    is_active: bool = True
    steps: list[ChainStepIn] = Field(min_length=1, max_length=20)


class ChainStepOut(BaseModel):
    id: uuid.UUID
    position: int
    name: str
    approver_user_id: uuid.UUID | None
    approver_role_id: uuid.UUID | None
    approver_name: str | None = None
    condition: dict[str, Any] | None
    approver_kind: str = "user"
    approver_ref: dict[str, Any] | None = None


class ChainOut(BaseModel):
    id: uuid.UUID
    subject_type: SubjectType
    name: str
    is_active: bool
    steps: list[ChainStepOut]
    created_at: datetime
    updated_at: datetime


class DecideIn(_In):
    action: DecideAction
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] | None = None
    # Epic 13 (R5): passed unchanged to the subject callbacks (``StepDecision.payload`` / ``FinalDecision``).
    subject_payload: dict[str, Any] | None = None


class DecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    step_name: str
    outcome: DecisionOutcome
    actor_id: uuid.UUID | None
    comment: str | None
    evaluated_condition: dict[str, Any] | None
    decided_at: datetime
    # Epic 13 (AD-24): the deciding actor.
    actor_kind: str = "user"
    attributed_kind: str | None = None
    attributed_id: uuid.UUID | None = None
    on_behalf_of: uuid.UUID | None = None


class SubjectSummary(BaseModel):
    label: str | None = None
    title: str | None = None


class RequestOut(BaseModel):
    id: uuid.UUID
    subject_type: str  # registry-validated since Epic 13
    subject_id: uuid.UUID
    subject: SubjectSummary | None = None
    chain_id: uuid.UUID | None
    chain_snapshot: list[dict[str, Any]]
    facts: dict[str, Any]
    status: RequestStatus
    current_position: int | None
    current_step_name: str | None = None
    current_approver_user_id: uuid.UUID | None
    current_approver_role_id: uuid.UUID | None
    current_approver_kind: str | None = None
    current_approver_client_user_id: uuid.UUID | None = None
    steps_source: str = "chain"
    submitted_by: uuid.UUID | None
    submitted_at: datetime
    completed_at: datetime | None
    decisions: list[DecisionOut] = Field(default_factory=list)
