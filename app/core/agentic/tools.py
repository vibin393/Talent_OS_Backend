# ruff: noqa: E501, D401
"""Controlled tool registry (Epic 53, AD-59, FR-266). Tools register only from
``app/services/agent_tools/<domain>/hooks.py`` (which loads every ``tools_*.py``).

``register_tool`` refuses (``ToolRegistryError``): a duplicate or malformed key, risk ``forbidden`` (★ actions are never
tools), an input model without ``extra="forbid"`` or with a reserved field name, a non-read tool without ``preview``,
an egress tool without ``send_intent`` or with risk ``read``. ``validate_catalogue`` runs after hooks autodiscovery and
reports human-only transitions and AgentSpecs naming unknown tools.
"""

import re
from collections.abc import Callable, Mapping

from app.core.agentic.errors import ToolRegistryError
from app.core.agentic.types import ToolSpec
from app.core.registry import Registry

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}\.[a-z][a-z0-9_]{1,40}$")
# NFR-46: the model can never name who acts, for whom or with what rights.
RESERVED_INPUT_FIELDS = frozenset({"tenant_id", "actor", "owner", "permission", "permissions", "scope"})
# Added by the runtime to every write tool's model-facing schema (spec 0.10) and stripped before validation.
RUNTIME_RESERVED_FIELDS = frozenset({"_rationale", "_confidence", "_evidence"})
INJECTED_TOOL_NAMES = frozenset({"emit_result", "await_event"})

_tools: Registry[str, ToolSpec] = Registry("agent tool")


def _check(spec: ToolSpec) -> None:
    if not KEY_RE.fullmatch(spec.key):
        msg = f"invalid tool key {spec.key!r} (expected <domain>.<verb_noun>)"
        raise ToolRegistryError(msg)
    if spec.risk == "forbidden":
        msg = f"tool {spec.key}: risk 'forbidden' cannot be registered (★ actions are never tools; see FORBIDDEN_ACTIONS)"
        raise ToolRegistryError(msg)
    if spec.risk not in ("read", "low", "high"):
        msg = f"tool {spec.key}: unknown risk {spec.risk!r}"
        raise ToolRegistryError(msg)
    if spec.version < 1 or not spec.owner_epic:
        msg = f"tool {spec.key}: version >= 1 and owner_epic are required"
        raise ToolRegistryError(msg)
    if len(spec.title) > 80 or len(spec.description) > 1024 or not spec.description.strip():
        msg = f"tool {spec.key}: title <= 80 and a description <= 1,024 characters are required"
        raise ToolRegistryError(msg)
    if spec.input_model.model_config.get("extra") != "forbid":
        msg = f"tool {spec.key}: input_model must set model_config extra='forbid'"
        raise ToolRegistryError(msg)
    fields = set(spec.input_model.model_fields)
    reserved = fields & (RESERVED_INPUT_FIELDS | RUNTIME_RESERVED_FIELDS) or {
        f for f in fields if f.startswith("_")
    }
    if reserved:
        msg = f"tool {spec.key}: reserved input field(s) {sorted(reserved)}"
        raise ToolRegistryError(msg)
    if spec.risk != "read" and spec.preview is None:
        msg = f"tool {spec.key}: a non-read tool needs a preview"
        raise ToolRegistryError(msg)
    if spec.egress and spec.risk == "read":
        msg = f"tool {spec.key}: an egress tool cannot be risk 'read'"
        raise ToolRegistryError(msg)
    if spec.egress and spec.send_intent is None:
        msg = f"tool {spec.key}: an egress tool needs send_intent"
        raise ToolRegistryError(msg)
    if spec.wire_name in INJECTED_TOOL_NAMES:
        msg = f"tool {spec.key}: wire name clashes with a runtime-injected tool"
        raise ToolRegistryError(msg)
    if spec.default_mode is not None and spec.default_mode not in ("off", "suggest", "auto"):
        msg = f"tool {spec.key}: invalid default_mode"
        raise ToolRegistryError(msg)
    if not 1 <= spec.max_calls_per_run <= 100 or not 1 <= spec.timeout_s <= 120:
        msg = f"tool {spec.key}: max_calls_per_run 1..100 and timeout_s 1..120"
        raise ToolRegistryError(msg)


def register_tool(spec: ToolSpec) -> None:
    _check(spec)
    if spec.key in _tools:
        msg = f"duplicate tool key {spec.key!r}"
        raise ToolRegistryError(msg)
    _tools.register(spec.key, spec)


def unregister_tool(key: str) -> None:
    """Tests only."""
    _tools.unregister(key)


def get_tool(key: str) -> ToolSpec:
    found = _tools.find(key)
    if found is None:
        raise KeyError(key)
    return found


def get_tool_by_wire(name: str) -> ToolSpec | None:
    if "__" not in name:
        return None
    return _tools.find(name.replace("__", ".", 1))


def tools() -> Mapping[str, ToolSpec]:
    return _tools.view()


def validate_catalogue(*, is_human_only: Callable[[str, str], bool]) -> list[str]:
    """Post-discovery checks: a tool whose ``transition`` is human-only, an AgentSpec / Copilot capability naming an
    unregistered tool. Returns the problems (empty = OK)."""
    from app.core.agentic.agents import agents
    from app.core.agentic.copilot import copilot_capabilities

    problems: list[str] = []
    for spec in _tools.view().values():
        if spec.transition is not None and is_human_only(*spec.transition):
            problems.append(f"tool {spec.key} performs the human-only transition {spec.transition}")
    for agent in agents().values():
        problems.extend(
            f"agent {agent.key} lists unknown tool {key}" for key in agent.tools if key not in _tools
        )
    for cap in copilot_capabilities().values():
        problems.extend(
            f"copilot capability {cap.key} lists unknown tool {key}" for key in cap.tools if key not in _tools
        )
    return problems


__all__ = [
    "INJECTED_TOOL_NAMES",
    "KEY_RE",
    "RESERVED_INPUT_FIELDS",
    "RUNTIME_RESERVED_FIELDS",
    "get_tool",
    "get_tool_by_wire",
    "register_tool",
    "tools",
    "unregister_tool",
    "validate_catalogue",
]
