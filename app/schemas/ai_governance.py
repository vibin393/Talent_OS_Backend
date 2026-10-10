"""Epic 65 DTOs: the AI governance console, enablement checklist, burn-in override and evaluation results
(spec-p4-governance A1-A5). Ids, labels, counts and costs only -- never Copilot / Analytics text."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.agentic import AgenticStateOut, RunOut, StepOut

ViolationKind = Literal["refused_tool", "injection_detected", "possible_cross_scope"]
TimelineKind = Literal["status", "model_call", "tool_call", "proposal", "decision"]
CheckStatus = Literal["ready", "missing", "warning", "not_applicable"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------- runs
class GovernanceRunQuery(BaseModel):
    agent_key: str | None = None
    status: str | None = None  # comma-separated
    origin: str | None = None
    trigger_kind: str | None = None
    subject_type: str | None = None
    subject_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    tainted: bool | None = None
    with_violations: bool | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)


class TimelineItem(_Frozen):
    at: datetime
    kind: TimelineKind
    ref: str | None
    label: str


class ConsoleStepOut(StepOut):
    model_tier: str | None = None
    cache_read_tokens: int | None = None
    cost_micros: int | None = None


class RunDetailOut(RunOut):
    steps: list[ConsoleStepOut]  # type: ignore[assignment]
    cost_usd: str
    payload_available: bool
    copilot_private: bool
    timeline: list[TimelineItem]


class ToolCallQuery(BaseModel):
    agent_key: str | None = None
    tool_key: str | None = None
    outcome: str | None = None
    risk: str | None = None
    mode: str | None = None
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    run_id: uuid.UUID | None = None
    target_type: str | None = None
    target_id: uuid.UUID | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)

    model_config = ConfigDict(populate_by_name=True)


class ToolCallRow(_Frozen):
    id: uuid.UUID
    run_id: uuid.UUID
    agent_key: str
    step_no: int
    tool_key: str
    risk: str
    mode: str
    mode_reason: str
    outcome: str
    error_code: str | None
    target_type: str | None
    target_id: uuid.UUID | None
    proposal_id: uuid.UUID | None
    egress: bool
    created_at: datetime


class ViolationQuery(BaseModel):
    kind: ViolationKind
    agent_key: str | None = None
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)

    model_config = ConfigDict(populate_by_name=True)


class ViolationRow(_Frozen):
    kind: ViolationKind
    run_id: uuid.UUID
    agent_key: str
    tool_key: str | None
    error_code: str | None
    mode_reason: str | None
    tainted: bool
    created_at: datetime


# ---------------------------------------------------------------- costs / summary
class CostQuery(BaseModel):
    group_by: Literal["agent", "day", "month"] = "agent"
    agent_key: str | None = None
    from_: date | None = Field(default=None, alias="from")
    to: date | None = None

    model_config = ConfigDict(populate_by_name=True)


class CostRow(_Frozen):
    key: str
    runs: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_micros: int
    cost_usd: str


class CostSeriesPoint(_Frozen):
    day: date
    agent_key: str
    cost_usd: str


class AgentBudgetRow(_Frozen):
    agent_key: str
    agent_name: str
    monthly_token_budget: int | None
    used_this_month: int
    pct_used: float | None
    expected_cost_per_run_micros: int | None
    actual_cost_per_run_micros: int | None


class MonthTotals(_Frozen):
    tokens: int
    cost_usd: str
    runs: int


class CostsOut(_Frozen):
    group_by: str
    rows: list[CostRow]
    totals: CostRow
    series: list[CostSeriesPoint]
    month: MonthTotals
    tenant_monthly_token_budget: int | None
    agents: list[AgentBudgetRow]


class GovernanceSummaryOut(_Frozen):
    runs_today: dict[str, int]
    pending_proposals: int
    violations_7d: int
    kill_switch: AgenticStateOut
    burn_in_agents: list[str]


# ---------------------------------------------------------------- settings
class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(max_length=500)


class CheckItemOut(_Frozen):
    key: str
    label: str
    status: CheckStatus
    detail: str
    fix_link: str | None
    blocking: bool


class ChecklistOut(_Frozen):
    agent_key: str
    ready: bool
    items: list[CheckItemOut]


class AgentLimitsOut(_Frozen):
    agent_key: str
    ceiling: dict[str, int]
    tenant_monthly_token_budget: int | None


# ---------------------------------------------------------------- evals
class EvalScore(_Frozen):
    score: str
    threshold: str
    passed: bool
    created_at: datetime
    model: str | None
    prompt_ref: str | None


class RedteamScore(_Frozen):
    cases_total: int
    cases_passed: int


class EvalRowOut(_Frozen):
    agent_key: str
    agent_name: str
    agent_version: int
    prompt_ref: str | None
    model: str | None
    offline: EvalScore | None
    live: EvalScore | None
    redteam: RedteamScore | None
    threshold: str
    status: Literal["passing", "below_threshold", "not_run"]
    failing_case_ids: list[str]
    last_run_at: datetime | None


__all__: list[Any] = [
    "AgentBudgetRow",
    "AgentLimitsOut",
    "CheckItemOut",
    "ChecklistOut",
    "ConsoleStepOut",
    "CostQuery",
    "CostRow",
    "CostSeriesPoint",
    "CostsOut",
    "EvalRowOut",
    "EvalScore",
    "GovernanceRunQuery",
    "GovernanceSummaryOut",
    "MonthTotals",
    "ReasonIn",
    "RedteamScore",
    "RunDetailOut",
    "TimelineItem",
    "ToolCallQuery",
    "ToolCallRow",
    "ViolationQuery",
    "ViolationRow",
]
