# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Agent registry (Epic 53, AD-72, FR-264). Owners register from ``app/services/agents/<key>/hooks.py``.

The foundation registers the 15 shipped keys as disabled placeholders (``placeholder=True``); ``register_agent``
replaces a placeholder of the same key once -- any other duplicate raises ``AgentRegistryError``. Extra triggers can
be added with ``register_agent_trigger`` (53.9).
"""

import dataclasses
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.agentic.errors import AgentRegistryError
from app.core.agentic.types import AutonomyMode, Effort, ModelTier, RunLimits, RunOrigin, TriggerKind
from app.core.permissions import Scope

AGENT_KEY_RE = re.compile(r"^_?[a-z][a-z0-9_]{1,39}$")
TRIGGER_KEY_RE = re.compile(r"^[a-z_]{3,40}$")

OwnerFn = Callable[..., Awaitable[uuid.UUID | None]]  # (ctx, db, subject) -> Accountable Owner user id


@dataclasses.dataclass(frozen=True, slots=True)
class TriggerSpec:
    key: str
    kind: TriggerKind
    event: str | None = None  # event class name, e.g. "ApplicationCreated"
    schedule: str | None = None  # cron (UTC), 5 fields
    subject: Callable[[Any], tuple[str, uuid.UUID] | None] | None = None
    dedup_key: Callable[[Any], str] | None = None  # default f"{event}:{subject}"
    on_active: Literal["attach", "drop"] = "drop"
    owner: OwnerFn | None = None
    # Module of the event class (``app.events.<x>``); defaults to a search of ``app.events`` at wiring time.
    event_module: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Kickoff:
    inputs: Mapping[str, str]
    untrusted: Mapping[str, str] = dataclasses.field(default_factory=dict)
    candidate_ids: tuple[uuid.UUID, ...] = ()


class EmitResult(BaseModel):
    """Default ``emit_result`` schema (the runtime-injected final tool)."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["done", "needs_human", "nothing_to_do"]
    summary: str = Field(max_length=2000)


@dataclasses.dataclass(frozen=True, slots=True)
class AgentSpec:
    key: str
    version: int
    owner_epic: str
    name: str
    purpose: str
    prompt_id: str
    model_tier: ModelTier = "standard"
    effort: Effort = "medium"
    tools: tuple[str, ...] = ()
    permission_ceiling: Mapping[str, Scope] = dataclasses.field(default_factory=dict)
    default_mode: AutonomyMode = "suggest"
    limits: RunLimits = RunLimits()  # noqa: RUF009 - frozen value object, safe as a shared default
    subject_type: str | None = None
    origin: RunOrigin = "agent"
    triggers: tuple[TriggerSpec, ...] = ()
    owner: OwnerFn | None = None
    kickoff: Callable[[Mapping[str, Any]], Kickoff] | None = None
    result_model: type[BaseModel] | None = None
    fallback: Literal["owner_task", "none"] = "owner_task"
    fallback_link: Callable[[tuple[str, uuid.UUID] | None], str] | None = None
    channels: tuple[str, ...] = ()
    consent_purposes: tuple[str, ...] = ("ai_processing",)
    aedt_feature: str | None = None
    eval_set: str | None = None
    eval_threshold: float = 0.9
    placeholder: bool = False

    @property
    def feature(self) -> str:
        """The AI feature toggle that enables this agent (``ai_tenant_settings.features``)."""
        return f"agent_{self.key}"


_agents: dict[str, AgentSpec] = {}
_extra_triggers: dict[str, list[TriggerSpec]] = {}


def _check(spec: AgentSpec) -> None:
    if not AGENT_KEY_RE.fullmatch(spec.key):
        msg = f"invalid agent key {spec.key!r}"
        raise AgentRegistryError(msg)
    if spec.version < 1 or not spec.owner_epic or not spec.prompt_id:
        msg = f"agent {spec.key}: version >= 1, owner_epic and prompt_id are required"
        raise AgentRegistryError(msg)
    if len(set(spec.tools)) != len(spec.tools):
        msg = f"agent {spec.key}: duplicate tool in allow-list"
        raise AgentRegistryError(msg)
    seen: set[str] = set()
    for trigger in spec.triggers:
        _check_trigger(spec.key, trigger, seen)
    if not 0.0 <= spec.eval_threshold <= 1.0:
        msg = f"agent {spec.key}: eval_threshold must be within 0..1"
        raise AgentRegistryError(msg)


def _check_trigger(agent_key: str, trigger: TriggerSpec, seen: set[str]) -> None:
    if not TRIGGER_KEY_RE.fullmatch(trigger.key) or trigger.key in seen:
        msg = f"agent {agent_key}: invalid or duplicate trigger key {trigger.key!r}"
        raise AgentRegistryError(msg)
    if trigger.kind == "event" and not trigger.event:
        msg = f"agent {agent_key}: event trigger {trigger.key} needs an event class name"
        raise AgentRegistryError(msg)
    if trigger.kind == "schedule" and (not trigger.schedule or len(trigger.schedule.split()) != 5):
        msg = f"agent {agent_key}: schedule trigger {trigger.key} needs a 5-field cron"
        raise AgentRegistryError(msg)
    seen.add(trigger.key)


def register_agent(spec: AgentSpec) -> None:
    """Register an agent; replaces a placeholder of the same key once, else a duplicate raises."""
    _check(spec)
    existing = _agents.get(spec.key)
    if existing is not None and not existing.placeholder:
        if spec.placeholder:
            return  # a placeholder never replaces a real definition (hooks import order does not matter)
        msg = f"duplicate agent key {spec.key!r}"
        raise AgentRegistryError(msg)
    _agents[spec.key] = spec


def unregister_agent(key: str) -> None:
    """Tests only."""
    _agents.pop(key, None)
    _extra_triggers.pop(key, None)


def get_agent(key: str) -> AgentSpec:
    found = _agents.get(key)
    if found is None:
        raise KeyError(key)
    return found


def agents() -> Mapping[str, AgentSpec]:
    return MappingProxyType(_agents)


def register_agent_trigger(agent_key: str, trigger: TriggerSpec) -> None:
    """53.9: add a trigger to a registered agent (owners' hooks)."""
    spec = _agents.get(agent_key)
    if spec is None:
        msg = f"unknown agent {agent_key!r}"
        raise AgentRegistryError(msg)
    seen = {t.key for t in agent_triggers(agent_key)}
    _check_trigger(agent_key, trigger, seen)
    _extra_triggers.setdefault(agent_key, []).append(trigger)


def agent_triggers(agent_key: str) -> tuple[TriggerSpec, ...]:
    spec = _agents.get(agent_key)
    base = spec.triggers if spec is not None else ()
    return (*base, *_extra_triggers.get(agent_key, ()))


__all__ = [
    "AgentSpec",
    "EmitResult",
    "Kickoff",
    "OwnerFn",
    "TriggerSpec",
    "agent_triggers",
    "agents",
    "get_agent",
    "register_agent",
    "register_agent_trigger",
    "unregister_agent",
]
