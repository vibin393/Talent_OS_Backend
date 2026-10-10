"""Workflow rule API models (Epic 10, story 10.4). Conditions and actions are JSON validated by the service
(``invalid_trigger`` / ``invalid_condition`` / ``invalid_action`` with a JSON path)."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # ``is_active`` in the body is ignored: new rules are inactive

    name: Name
    description: Annotated[str, StringConstraints(max_length=2000)] | None = None
    trigger: Annotated[str, StringConstraints(max_length=64)]
    conditions: dict[str, Any] = Field(default_factory=dict)
    actions: list[Any] = Field(default_factory=list)
    # Epic 32: a v2 graph (mutually exclusive with conditions / actions, 422 ``graph_and_form``).
    graph: dict[str, Any] | None = None


class RuleUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: Name | None = None
    description: Annotated[str, StringConstraints(max_length=2000)] | None = None
    trigger: Annotated[str, StringConstraints(max_length=64)] | None = None
    conditions: dict[str, Any] | None = None
    actions: list[Any] | None = None
    graph: dict[str, Any] | None = None


class OutlineRow(BaseModel):
    depth: int
    node_id: str
    kind: str
    text: str


class RuleOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    trigger: str
    conditions: dict[str, Any]
    actions: list[dict[str, Any]]
    is_active: bool
    version: int
    created_by: uuid.UUID
    updated_by: uuid.UUID | None
    last_fired_at: datetime | None
    archived_at: datetime | None
    failures_24h: int
    created_at: datetime
    updated_at: datetime
    # Epic 32: the graph (stored, or derived from the P1 form), its outline and whether ``to_p1`` holds.
    graph: dict[str, Any] | None = None
    definition_version: int = 1
    outline: list[OutlineRow] = Field(default_factory=list)
    step_count: int = 0
    is_p1_form: bool = True
    waiting_runs: int = 0


class WaitOut(BaseModel):
    id: uuid.UUID
    node_id: str
    kind: str
    status: str
    outcome: str | None
    resume_at: datetime | None
    timeout_at: datetime | None
    approval_request_id: uuid.UUID | None
    created_at: datetime


class ExecutionOut(BaseModel):
    id: uuid.UUID
    rule_id: uuid.UUID
    rule_version: int
    mode: str
    trigger: str
    event_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    chain_depth: int
    status: str
    condition_trace: list[dict[str, Any]]
    action_results: list[dict[str, Any]]
    enqueued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    created_at: datetime
    node_trace: list[dict[str, Any]] = Field(default_factory=list)
    waits: list[WaitOut] = Field(default_factory=list)


class DryRunIn(BaseModel):
    entity_id: uuid.UUID
    extras: dict[str, Any] = Field(default_factory=dict)
    assume_approval: Literal["approved", "rejected", "timeout"] = "approved"


class PathStep(BaseModel):
    node_id: str
    type: str
    outcome: str
    scheduled_at: datetime | None = None


class DraftDryRunIn(DryRunIn):
    rule: RuleIn


class DryRunOut(BaseModel):
    matched: bool
    entity_type: str
    entity_id: uuid.UUID
    condition_trace: list[dict[str, Any]]
    action_results: list[dict[str, Any]]
    execution_id: uuid.UUID | None
    note: str = "Nothing was executed or sent."
    path: list[PathStep] = Field(default_factory=list)


class FieldOut(BaseModel):
    path: str
    label: str
    type: str
    values: list[str]
    ops: list[str]


class TriggerOut(BaseModel):
    key: str
    label: str
    entity_type: str
    available: bool
    fields: list[FieldOut]
    group: str = "Phase 1"
    owner_epic: str | None = None
    # Epic 48 (decision 9): when set, the builder offers only these action types for the trigger.
    allowed_actions: list[str] | None = None


class ActionTypeOut(BaseModel):
    type: str
    label: str
    params_schema: dict[str, Any]
    available: bool = True
    entity_types: list[str] = Field(default_factory=list)


class ContractActionOut(BaseModel):
    type: str
    owner_epic: str
    available: bool


class PolicyRef(BaseModel):
    id: uuid.UUID
    name: str


class RegistryOut(BaseModel):
    triggers: list[TriggerOut]
    ops: list[str]
    actions: list[ActionTypeOut]
    move_stage_blocked_targets: list[str]
    limits: dict[str, int]
    # Epic 32
    node_types: list[str] = Field(default_factory=list)
    delay_units: list[str] = Field(default_factory=list)
    approver_kinds: list[str] = Field(default_factory=list)
    max_steps: int = 25
    contract_actions: list[ContractActionOut] = Field(default_factory=list)
    escalation_policies: list[PolicyRef] = Field(default_factory=list)
