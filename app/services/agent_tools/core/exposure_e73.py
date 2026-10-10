# ruff: noqa: E501
"""Epic 73 (Governed MCP Server, spec 0.3; FR-377): MCP exposures of the ``core`` tools. Loaded by the domain's
``hooks.py`` through ``agent_tools._loader`` (``exposure_*.py``); validated post-discovery (``validate_exposures``)."""

from app.core.agentic.exposure import ToolExposure, expose_tool, exposures

EXPOSURES: tuple[tuple[str, str, str], ...] = (
    ("knowledge.search", "mcp:read", "knowledge"),
    ("notes.add", "mcp:draft", "core"),
    ("tasks.create", "mcp:draft", "core"),
    ("messaging.create_draft", "mcp:draft", "outreach"),
    ("messaging.send", "mcp:propose", "outreach"),
)

for _key, _scope, _group in EXPOSURES:
    if _key not in exposures():
        expose_tool(ToolExposure(_key, frozenset({"mcp"}), _scope, (), _group, max_results=50))  # type: ignore[arg-type]
