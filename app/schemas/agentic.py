# ruff: noqa: E501
"""Phase 4 shared DTOs (Epic 53, spec 0.7 / 0.8): gateway step request / result and the agent runtime facade DTOs.
Imported by ``ai.loop``, ``agent_runtime`` and agent packages; no service imports."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ModelTier = Literal["fast", "standard", "deep"]
Effort = Literal["low", "medium", "high"]
AutonomyMode = Literal["off", "suggest", "auto"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# ================================================================ gateway step (ai.loop)
class ModelToolDef(_Frozen):
    name: str
    description: str
    input_schema: dict[str, Any]
    strict: bool = True


class StepRequest(_Frozen):
    agent_key: str
    run_id: uuid.UUID
    step_no: int
    model_tier: ModelTier
    effort: Effort
    system: str
    tools: tuple[ModelToolDef, ...]
    messages: tuple[dict[str, Any], ...]
    max_tokens: int = 16_000
    subject: tuple[str, uuid.UUID] | None = None
    user_started: bool = False


class ToolUse(_Frozen):
    id: str
    name: str
    input: dict[str, Any]


class StepUsage(_Frozen):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class StepResult(_Frozen):
    ok: bool
    content: tuple[dict[str, Any], ...] = ()
    tool_uses: tuple[ToolUse, ...] = ()
    text: str = ""
    stop_reason: str | None = None
    refusal_category: str | None = None
    usage: StepUsage = StepUsage()
    model: str | None = None
    action_id: uuid.UUID | None = None
    # global_kill | kill_switch | feature_disabled | budget_exceeded | agent_budget | consent_missing |
    # not_configured | timeout | provider_error | truncated | refused
    error_code: str | None = None


# ================================================================ runtime facade
class RunRef(_Frozen):
    run_id: uuid.UUID | None
    status: str
    reason: str | None = None


class RunQuery(BaseModel):
    agent_key: str | None = None
    status: str | None = None
    origin: str | None = None
    subject_type: str | None = None
    subject_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)


class RunSummary(_Frozen):
    id: uuid.UUID
    agent_key: str
    agent_name: str
    origin: str
    trigger_kind: str
    trigger_key: str | None
    status: str
    reason_code: str | None
    subject_type: str | None
    subject_id: uuid.UUID | None
    owner_user_id: uuid.UUID | None  # Epic 66: NULL for portal-owned runs
    actor_label: str
    step_count: int
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    cost_micros: int
    tainted: bool
    policy_violations: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    queue_age_seconds: int | None


class StepOut(_Frozen):
    step_no: int
    status: str
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    ai_action_id: uuid.UUID | None


class ToolCallOut(_Frozen):
    id: uuid.UUID
    step_no: int
    tool_key: str
    tool_version: int
    risk: str
    mode: str
    mode_reason: str
    outcome: str
    error_code: str | None
    proposal_id: uuid.UUID | None
    egress: bool
    target_type: str | None
    target_id: uuid.UUID | None
    args_redacted: dict[str, Any] | None
    result_summary: str | None
    latency_ms: int | None


class ProposalSummary(_Frozen):
    id: uuid.UUID
    human_id: str
    tool_key: str
    title: str
    status: str
    risk: str


class RunOut(RunSummary):
    related_refs: list[Any]
    candidate_ids: list[uuid.UUID]
    limits: dict[str, Any]
    wait_condition: str | None
    wait_until: datetime | None
    result: dict[str, Any] | None
    fallback_task_id: uuid.UUID | None
    steps: list[StepOut]
    tool_calls: list[ToolCallOut]
    proposals: list[ProposalSummary]


class PayloadReadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=10, max_length=500)


class PayloadItem(_Frozen):
    kind: str
    step_no: int
    tool_call_id: uuid.UUID | None
    content: Any
    sha256: str
    created_at: datetime


class RunPayloadOut(_Frozen):
    run_id: uuid.UUID
    items: list[PayloadItem]


class RunStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_key: str = Field(max_length=40)
    subject_type: str = Field(max_length=40)
    subject_id: uuid.UUID


class RunCancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
    reason_code: Literal["manual", "alternative_process", "subject_closed"] = "manual"


# ---------------------------------------------------------------- proposals
class ProposalQuery(BaseModel):
    status: str | None = None
    mine: bool = False
    agent_key: str | None = None
    tool_key: str | None = None
    target_type: str | None = None
    target_id: uuid.UUID | None = None
    older_than_hours: int | None = Field(default=None, ge=1)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)


class ProposalOut(_Frozen):
    id: uuid.UUID
    human_id: str
    origin: str
    agent_key: str
    agent_name: str
    tool_key: str
    tool_title: str
    title: str
    preview: dict[str, Any]
    risk: str
    rationale: str | None
    confidence: Decimal | None
    evidence: list[Any]
    target_type: str | None
    target_id: uuid.UUID | None
    status: str
    edited: bool
    assignee_user_id: uuid.UUID | None
    route_role_key: str | None
    owner_user_id: uuid.UUID | None  # Epic 66: NULL for portal-owned Drafts
    expires_at: datetime
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    decision_note: str | None
    executed_at: datetime | None
    error_code: str | None
    run_id: uuid.UUID | None
    can_decide: bool
    args: dict[str, Any] | None = None
    input_schema: dict[str, Any] | None = None
    created_at: datetime | None = None  # Epic 65 (additive): inbox ageing buckets


class ProposalDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=1000)
    edited_args: dict[str, Any] | None = None


class BulkDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)
    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=1000)


class ConfirmDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edited_args: dict[str, Any] | None = None


class ModeDecision(_Frozen):
    mode: AutonomyMode
    risk: str
    # global_kill | kill_switch | feature_off | agent_off | not_allowlisted | no_permission | burn_in | policy |
    # ceiling | tainted_egress | copilot_draft | untemplated_send | suspicious_input
    reason: str


# ---------------------------------------------------------------- configuration
class AgentConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_mode: AutonomyMode | None = None
    monthly_token_budget: int | None = Field(default=None, ge=0)
    limits: dict[str, int] | None = None
    triggers_enabled: dict[str, bool] | None = None
    channels: list[Literal["email", "sms", "whatsapp", "portal", "in_app"]] | None = None
    working_hours: dict[str, Any] | None = None
    proposal_expiry_hours: int | None = Field(default=None, ge=1, le=720)


class ActionPolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: AutonomyMode | None = None
    route_role_key: str | None = Field(default=None, max_length=60)


class AgentEnabledIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class EnablementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notice_version: str = Field(max_length=20)


class KillSwitchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: bool
    reason: str | None = Field(default=None, max_length=500)


class PlatformKillSwitchIn(KillSwitchIn):
    tenant_id: uuid.UUID | None = None


class AgentTriggerOut(_Frozen):
    key: str
    kind: str
    enabled: bool


class AgentToolModeOut(_Frozen):
    key: str
    title: str
    risk: str
    mode: str
    mode_reason: str
    route_role_key: str | None
    permissions: list[str]


class AgentCatalogOut(_Frozen):
    key: str
    name: str
    purpose: str
    owner_epic: str
    version: int
    placeholder: bool
    enabled: bool
    model_tier: str
    default_mode: str
    burn_in_until: datetime | None
    limits: dict[str, int]
    monthly_token_budget: int | None
    used_this_month: int
    permission_ceiling: dict[str, str]
    triggers: list[AgentTriggerOut]
    tools: list[AgentToolModeOut]
    eval_set: str | None
    eval_threshold: float


class AgenticStateOut(_Frozen):
    enabled: bool
    notice_version: str | None
    current_notice_version: str
    enabled_at: datetime | None
    kill_switch: bool
    kill_switch_at: datetime | None
    reason: str | None
    platform_kill_switch: bool
    global_kill_switch: bool


class ToolCatalogOut(_Frozen):
    key: str
    version: int
    owner_epic: str
    title: str
    description: str
    risk: str
    permissions: list[str]
    egress: bool
    resource: str
    default_mode: str | None
    input_schema: dict[str, Any]
    forbidden: bool = False


# ================================================================ Epic 66 (§0.15): external (MCP) sessions
class ExternalSessionIn(BaseModel):
    installation_id: uuid.UUID
    user_id: uuid.UUID
    token_id: uuid.UUID
    client_name: str = Field(max_length=500)
    mcp_session_id: str | None = Field(default=None, max_length=200)


class ExternalCallIn(BaseModel):
    run_id: uuid.UUID
    tool_key: str = Field(min_length=3, max_length=80)
    args: dict[str, Any] = Field(default_factory=dict)
    call_id: str = Field(min_length=1, max_length=64)  # JSON-RPC id: idempotency within the session


class ExternalCallOut(BaseModel):
    outcome: Literal["executed", "proposed", "refused", "invalid", "not_found", "failed", "deduplicated"]
    result: dict[str, Any] | None = None
    proposal_id: uuid.UUID | None = None
    proposal_url: str | None = None
    error_code: str | None = None
    truncated: bool = False
    # D-25(b): candidates touched / returned by an executed call (Epic 73's bulk-read detector).
    candidate_ids: list[uuid.UUID] = Field(default_factory=list)


__all__ = [
    "ActionPolicyIn",
    "AgentCatalogOut",
    "AgentConfigIn",
    "AgentEnabledIn",
    "AgentToolModeOut",
    "AgentTriggerOut",
    "AgenticStateOut",
    "BulkDecisionIn",
    "ConfirmDraftIn",
    "EnablementIn",
    "ExternalCallIn",
    "ExternalCallOut",
    "ExternalSessionIn",
    "KillSwitchIn",
    "ModeDecision",
    "ModelToolDef",
    "PayloadItem",
    "PayloadReadIn",
    "PlatformKillSwitchIn",
    "ProposalDecisionIn",
    "ProposalOut",
    "ProposalQuery",
    "ProposalSummary",
    "RunCancelIn",
    "RunOut",
    "RunPayloadOut",
    "RunQuery",
    "RunRef",
    "RunStartIn",
    "RunSummary",
    "StepOut",
    "StepRequest",
    "StepResult",
    "StepUsage",
    "ToolCallOut",
    "ToolCatalogOut",
    "ToolUse",
]
