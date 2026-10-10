# ruff: noqa: E501
"""Epic 73 (Governed MCP Server, spec 0.3; FR-377): MCP exposures of the ``navigation`` tools. Loaded by the domain's
``hooks.py`` through ``agent_tools._loader`` (``exposure_*.py``); validated post-discovery (``validate_exposures``)."""

from app.core.agentic.exposure import ToolExposure, expose_tool, exposures

EXPOSURES: tuple[tuple[str, str, str], ...] = (
    ("navigation.screen_for", "mcp:read", "core"),
)

for _key, _scope, _group in EXPOSURES:
    if _key not in exposures():
        expose_tool(ToolExposure(_key, frozenset({"mcp"}), _scope, (), _group, max_results=50))  # type: ignore[arg-type]
