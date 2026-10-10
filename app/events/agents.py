"""Agent runtime events (Epic 53, spec 0.4). Ids and scalars only; no PII, no prompt text."""

import dataclasses
import uuid
from datetime import datetime


@dataclasses.dataclass(frozen=True, slots=True)
class AgentRunStarted:
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    agent_key: str
    origin: str
    trigger_kind: str
    subject_type: str | None
    subject_id: uuid.UUID | None
    owner_user_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class AgentRunFinished:
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    agent_key: str
    status: str  # completed | stopped | cancelled
    reason_code: str | None
    subject_type: str | None
    subject_id: uuid.UUID | None
    owner_user_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class AgentRunFailed:
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    agent_key: str
    reason_code: str
    subject_type: str | None
    subject_id: uuid.UUID | None
    owner_user_id: uuid.UUID
    fallback_task_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class ProposalCreated:
    tenant_id: uuid.UUID
    proposal_id: uuid.UUID
    run_id: uuid.UUID | None
    agent_key: str
    tool_key: str
    origin: str
    target_type: str | None
    target_id: uuid.UUID | None
    assignee_user_id: uuid.UUID | None
    route_role_key: str | None
    expires_at: datetime


@dataclasses.dataclass(frozen=True, slots=True)
class ProposalDecided:
    tenant_id: uuid.UUID
    proposal_id: uuid.UUID
    run_id: uuid.UUID | None
    agent_key: str
    tool_key: str
    outcome: str  # approved | edited_approved | rejected | expired | stale | discarded
    status: str
    decided_by: uuid.UUID | None
    target_type: str | None
    target_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class KillSwitchChanged:
    tenant_id: uuid.UUID
    scope: str  # tenant | platform_tenant | global
    on: bool
    actor_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class AgentBudgetThresholdCrossed:
    tenant_id: uuid.UUID
    agent_key: str
    period: str
    threshold: int
    used: int
    budget: int


__all__ = [
    "AgentBudgetThresholdCrossed",
    "AgentRunFailed",
    "AgentRunFinished",
    "AgentRunStarted",
    "KillSwitchChanged",
    "ProposalCreated",
    "ProposalDecided",
]
