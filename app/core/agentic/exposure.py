# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Tool exposure registry (Epic 66, spec §0.15, AD-84 / AD-89). Pure: imports nothing from ``app.services``.

An exposure makes a registered agent tool callable from an external surface: MCP (Epic 73) or a portal Copilot
(Epic 77). Owners call ``expose_tool`` from their ``agent_tools`` hooks; ``validate_exposures`` runs after hooks
autodiscovery (``agent_runtime.hooks``) and startup fails on any problem:

* the tool is unknown;
* an MCP exposure without ``mcp_scope``; risk ``high`` with ``mcp_scope != "mcp:propose"``; risk ``low`` with
  ``mcp_scope == "mcp:read"``; an egress tool exposed with anything but ``mcp:propose`` (never direct, FR-377);
* a portal surface on a tool whose key does not start with ``portal_``;
* ``ToolSpec.mcp_exposed`` disagreeing with the presence of an ``mcp`` exposure (both directions).

★ actions are impossible here: they are never registered as tools.
"""

import dataclasses
from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, get_args

from app.core.agentic.errors import ToolRegistryError

Surface = Literal["mcp", "portal_client", "portal_vendor", "portal_candidate"]
McpScope = Literal["mcp:read", "mcp:draft", "mcp:propose"]
SURFACES: frozenset[str] = frozenset(get_args(Surface))
MCP_SCOPES: frozenset[str] = frozenset(get_args(McpScope))
PORTAL_SURFACES: frozenset[str] = SURFACES - {"mcp"}
PORTAL_TOOL_PREFIX = "portal_"


@dataclasses.dataclass(frozen=True, slots=True)
class ToolExposure:
    tool_key: str
    surfaces: frozenset[Surface]
    mcp_scope: McpScope | None = None  # required when "mcp" in surfaces
    resource_scopes: tuple[str, ...] = ()  # e.g. ("candidates:read",): the token must hold each one
    group: str = "core"  # tenant-enabled MCP tool group (FR-377)
    portal_permissions: tuple[
        str, ...
    ] = ()  # portal keys required (principal.portal_permissions) for portal surfaces
    max_results: int = 50  # FR-379 cap


_exposures: dict[str, ToolExposure] = {}


def _check(exp: ToolExposure) -> None:
    if not exp.tool_key:
        msg = "exposure: tool_key is required"
        raise ToolRegistryError(msg)
    surfaces = frozenset(exp.surfaces)
    if not surfaces or not surfaces <= SURFACES:
        msg = f"exposure {exp.tool_key}: surfaces must be a non-empty subset of {sorted(SURFACES)}"
        raise ToolRegistryError(msg)
    if exp.mcp_scope is not None and exp.mcp_scope not in MCP_SCOPES:
        msg = f"exposure {exp.tool_key}: unknown mcp_scope {exp.mcp_scope!r}"
        raise ToolRegistryError(msg)
    if "mcp" in surfaces and exp.mcp_scope is None:
        msg = f"exposure {exp.tool_key}: mcp_scope is required for the mcp surface"
        raise ToolRegistryError(msg)
    if not 1 <= exp.max_results <= 1000:
        msg = f"exposure {exp.tool_key}: max_results must be within 1..1000"
        raise ToolRegistryError(msg)
    if not exp.group or len(exp.group) > 40:
        msg = f"exposure {exp.tool_key}: group must be 1..40 characters"
        raise ToolRegistryError(msg)


def expose_tool(exp: ToolExposure) -> None:
    """Register an exposure (one per tool key; a duplicate raises ``ToolRegistryError``)."""
    _check(exp)
    if exp.tool_key in _exposures:
        msg = f"duplicate exposure for tool {exp.tool_key!r}"
        raise ToolRegistryError(msg)
    _exposures[exp.tool_key] = exp


def unexpose_tool(tool_key: str) -> None:
    """Tests only."""
    _exposures.pop(tool_key, None)


def get_exposure(tool_key: str) -> ToolExposure | None:
    return _exposures.get(tool_key)


def exposures(surface: Surface | None = None) -> Mapping[str, ToolExposure]:
    """Exposures having ``surface`` (every exposure when ``None``), keyed by tool key."""
    if surface is None:
        return MappingProxyType(dict(_exposures))
    return MappingProxyType({k: e for k, e in _exposures.items() if surface in e.surfaces})


def validate_exposures() -> list[str]:
    """Post-discovery checks (module docstring). Returns the problems (empty = OK)."""
    from app.core.agentic.tools import tools

    catalogue = tools()
    problems: list[str] = []
    for key, exp in sorted(_exposures.items()):
        tool = catalogue.get(key)
        if tool is None:
            problems.append(f"exposure {key}: unknown tool")
            continue
        if "mcp" in exp.surfaces:
            if tool.risk == "high" and exp.mcp_scope != "mcp:propose":
                problems.append(f"exposure {key}: a high-risk tool needs mcp_scope 'mcp:propose'")
            if tool.risk == "low" and exp.mcp_scope == "mcp:read":
                problems.append(f"exposure {key}: a low-risk tool cannot use mcp_scope 'mcp:read'")
            if tool.egress and exp.mcp_scope != "mcp:propose":
                problems.append(
                    f"exposure {key}: an egress tool needs mcp_scope 'mcp:propose' (never direct)"
                )
        if exp.surfaces & PORTAL_SURFACES and not key.startswith(PORTAL_TOOL_PREFIX):
            problems.append(
                f"exposure {key}: a portal surface needs a portal tool (key prefix '{PORTAL_TOOL_PREFIX}')"
            )
    mcp_keys = {k for k, e in _exposures.items() if "mcp" in e.surfaces}
    for key, tool in sorted(catalogue.items()):
        if tool.mcp_exposed and key not in mcp_keys:
            problems.append(f"tool {key}: mcp_exposed=True without an mcp exposure")
        if not tool.mcp_exposed and key in mcp_keys:
            problems.append(f"tool {key}: an mcp exposure requires mcp_exposed=True")
    return problems


def check_exposures() -> None:
    """Post-discovery hook body: raise when ``validate_exposures`` reports a problem."""
    problems = validate_exposures()
    if problems:
        msg = "tool exposure registry is invalid: " + "; ".join(problems)
        raise RuntimeError(msg)


__all__ = [
    "MCP_SCOPES",
    "PORTAL_SURFACES",
    "PORTAL_TOOL_PREFIX",
    "SURFACES",
    "McpScope",
    "Surface",
    "ToolExposure",
    "check_exposures",
    "expose_tool",
    "exposures",
    "get_exposure",
    "unexpose_tool",
    "validate_exposures",
]
