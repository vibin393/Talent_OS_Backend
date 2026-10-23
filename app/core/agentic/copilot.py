# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Copilot capability registry (Epic 53 contract, AD-64). Epic 54 registers the base capability and Epic 64 the
``analytics`` one, so 64 builds in parallel with 54 without importing Copilot code."""

import dataclasses
import re
from collections.abc import Callable, Mapping
from types import MappingProxyType

from app.core.agentic.errors import AgentRegistryError

CAPABILITY_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


@dataclasses.dataclass(frozen=True, slots=True)
class CopilotCapability:
    key: str
    owner_epic: str
    title: str
    description: str
    tools: tuple[str, ...]  # added to the copilot allow-list when the capability is on
    prompt_section: str | None = None  # prompt file appended to the static copilot system prompt
    # C-4 gating: on when the owning agent's AI feature (``agent_<agent_key>``) is enabled for the tenant AND the user
    # holds ``permission`` (None = no extra permission).
    agent_key: str = "copilot"
    permission: str | None = None


_capabilities: dict[str, CopilotCapability] = {}


def register_copilot_capability(cap: CopilotCapability) -> None:
    if not CAPABILITY_KEY_RE.fullmatch(cap.key) or not cap.owner_epic:
        msg = f"invalid copilot capability {cap.key!r}"
        raise AgentRegistryError(msg)
    if cap.key in _capabilities:
        msg = f"duplicate copilot capability {cap.key!r}"
        raise AgentRegistryError(msg)
    _capabilities[cap.key] = cap


def unregister_copilot_capability(key: str) -> None:
    """Tests only."""
    _capabilities.pop(key, None)


def copilot_capabilities() -> Mapping[str, CopilotCapability]:
    return MappingProxyType(_capabilities)


def enabled_capabilities(
    features: Mapping[str, bool], has_permission: Callable[[str], bool]
) -> list[CopilotCapability]:
    """C-4: the capabilities that are on for a tenant's AI ``features`` and a user (``has_permission(key)``)."""
    return [
        cap
        for cap in _capabilities.values()
        if features.get(f"agent_{cap.agent_key}", False)
        and (cap.permission is None or has_permission(cap.permission))
    ]


__all__ = [
    "CopilotCapability",
    "copilot_capabilities",
    "enabled_capabilities",
    "register_copilot_capability",
    "unregister_copilot_capability",
]
