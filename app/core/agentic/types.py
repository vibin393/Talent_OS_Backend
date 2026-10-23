# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Phase 4 agentic contract types (Epic 53, spec 0.5, AD-59/AD-61). Pure: imports nothing from ``app.services``.

Risk classes are ``read | low | high``; ``forbidden`` exists only as the class ``register_tool`` refuses (★ actions are
never tools -- ``FORBIDDEN_ACTIONS`` documents them). Autonomy modes are ordered ``off < suggest < auto``; a risk
class caps the mode (``RISK_CEILING``).
"""

import dataclasses
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.context import RequestContext

RiskClass = Literal["read", "low", "high", "forbidden"]
AutonomyMode = Literal["off", "suggest", "auto"]
MODE_ORDER: dict[str, int] = {"off": 0, "suggest": 1, "auto": 2}
RISK_ORDER: dict[str, int] = {"read": 0, "low": 1, "high": 2, "forbidden": 3}
RISK_CEILING: dict[str, AutonomyMode] = {"read": "auto", "low": "auto", "high": "suggest"}
ModelTier = Literal["fast", "standard", "deep"]
Effort = Literal["low", "medium", "high"]
# Epic 66 (§0.15): ``external`` = MCP sessions (AD-84), ``portal`` = portal-owned Copilot runs (AD-89).
RunOrigin = Literal["agent", "copilot", "conversation", "external", "portal"]
TriggerKind = Literal["event", "schedule", "workflow", "manual", "copilot", "inbound", "external", "portal"]
WaitCondition = str  # "reply" | "slot_pick" | "proposal_decided" | agent-defined, ^[a-z_]{3,40}$
WAIT_CONDITION_RE = re.compile(r"^[a-z_]{3,40}$")


def max_risk(a: RiskClass, b: RiskClass) -> RiskClass:
    return a if RISK_ORDER[a] >= RISK_ORDER[b] else b


def min_mode(a: AutonomyMode, b: AutonomyMode) -> AutonomyMode:
    return a if MODE_ORDER[a] <= MODE_ORDER[b] else b


@dataclasses.dataclass(frozen=True, slots=True)
class RunLimits:
    """Platform ceilings = these defaults; tenants may only lower them (FR-267, FR-271)."""

    max_steps: int = 12
    max_tool_calls: int = 30
    max_writes: int = 10
    max_tokens_per_run: int = 150_000
    step_timeout_s: int = 90
    wall_clock_s: int = 900
    max_parallel_tool_calls: int = 8
    max_wait_hours: int = 168

    def narrowed(self, overrides: Mapping[str, int]) -> "RunLimits":
        """A copy with ``overrides`` applied; ``ValueError`` if a key is unknown or a value is < 1 or above the
        ceiling (``max_wait_hours`` may be 0 = no waits)."""
        changes: dict[str, int] = {}
        for key, raw in overrides.items():
            if key not in self.__dataclass_fields__:
                msg = f"unknown limit {key!r}"
                raise ValueError(msg)
            value = int(raw)
            ceiling = int(getattr(self, key))
            floor = 0 if key == "max_wait_hours" else 1
            if value < floor or value > ceiling:
                msg = f"limit {key} must be between {floor} and {ceiling}"
                raise ValueError(msg)
            changes[key] = value
        return dataclasses.replace(self, **changes)

    def to_json(self) -> dict[str, int]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class ScopeRule:
    resource: (
        str  # "candidate" | "application" | "demand" | "interview" | "placement" | "client" | ... | "none"
    )
    target_arg: str | None = None
    cross_subject: bool = False
    candidate_arg: str | None = None
    version: Callable[[Any], str] | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class IdempotencyRule:
    fields: tuple[str, ...] = ()
    per_run: bool = True


@dataclasses.dataclass(frozen=True, slots=True)
class Recipient:
    kind: Literal["candidate", "worker", "contact", "client_user", "user", "provider_contact"]
    id: uuid.UUID
    channel: Literal["email", "sms", "whatsapp", "portal", "in_app"]


@dataclasses.dataclass(frozen=True, slots=True)
class SendIntent:
    recipients: tuple[Recipient, ...]
    templated: bool
    template_ref: str | None
    grounded: bool = False
    conversation_id: uuid.UUID | None = None
    citations: tuple[str, ...] = ()
    text_for_checks: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class ProposalPreview:
    title: str
    summary: str
    fields: tuple[tuple[str, str], ...] = ()
    renderer: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title[:200],
            "summary": self.summary[:4000],
            "fields": [[k, v] for k, v in self.fields],
            "renderer": self.renderer,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class ToolOutcome:
    data: BaseModel
    refs: tuple[tuple[str, uuid.UUID], ...] = ()
    candidate_ids: tuple[uuid.UUID, ...] = ()
    target: tuple[str, uuid.UUID] | None = None
    summary: str = ""


class ToolContext(Protocol):
    request: RequestContext
    db: AsyncSession
    actor: ActorRef
    run_id: uuid.UUID | None
    agent_key: str
    owner_user_id: uuid.UUID
    subject: tuple[str, uuid.UUID] | None
    idempotency_key: str
    execution: Literal["agent", "approved_proposal", "copilot_confirm"]


ToolHandler = Callable[[ToolContext, Any], Awaitable[ToolOutcome]]
PreviewFn = Callable[[ToolContext, Any], Awaitable[ProposalPreview]]
SendIntentFn = Callable[[ToolContext, Any], Awaitable[SendIntent]]


@dataclasses.dataclass(frozen=True, slots=True)
class ToolSpec:
    key: str
    version: int
    owner_epic: str
    title: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    permissions: tuple[str, ...]
    risk: RiskClass
    scope: ScopeRule
    handler: ToolHandler
    risk_fn: Callable[[Any], RiskClass] | None = None
    idempotency: IdempotencyRule = IdempotencyRule()  # noqa: RUF009
    untrusted_output: frozenset[str] = frozenset()
    untrusted_input: frozenset[str] = frozenset()
    egress: bool = False
    send_intent: SendIntentFn | None = None
    transition: tuple[str, str] | None = None
    preview: PreviewFn | None = None
    default_mode: AutonomyMode | None = None
    max_calls_per_run: int = 20
    timeout_s: int = 20
    # Epic 66 (§0.15): True <=> an exposure with surface "mcp" exists (``core.agentic.exposure``; post-discovery check).
    mcp_exposed: bool = False

    @property
    def wire_name(self) -> str:
        return self.key.replace(".", "__")

    def effective_risk(self, args: Any | None) -> RiskClass:
        """``max(risk, risk_fn(args))`` -- ``risk_fn`` can only raise the class."""
        if args is None or self.risk_fn is None:
            return self.risk
        return max_risk(self.risk, self.risk_fn(args))


# Documented ★ actions agents can never perform (governance console + tests). They are never tools.
FORBIDDEN_ACTIONS: tuple[tuple[str, str], ...] = (
    ("application", "rejected"),
    ("submission", "sent"),
    ("offer", "released"),
    ("interview_stage", "outcome"),
    ("invoice", "issued"),
    ("payment", "recorded"),
    ("screening", "outcome"),
    ("scorecard", "submitted"),
    ("skill_evidence", "verified"),
    ("agent_proposal", "decided"),
    ("copilot_draft", "confirmed"),
    ("agent_settings", "changed"),
)


__all__ = [
    "FORBIDDEN_ACTIONS",
    "MODE_ORDER",
    "RISK_CEILING",
    "RISK_ORDER",
    "WAIT_CONDITION_RE",
    "AutonomyMode",
    "Effort",
    "IdempotencyRule",
    "ModelTier",
    "PreviewFn",
    "ProposalPreview",
    "Recipient",
    "RiskClass",
    "RunLimits",
    "RunOrigin",
    "ScopeRule",
    "SendIntent",
    "SendIntentFn",
    "ToolContext",
    "ToolHandler",
    "ToolOutcome",
    "ToolSpec",
    "TriggerKind",
    "WaitCondition",
    "max_risk",
    "min_mode",
]
