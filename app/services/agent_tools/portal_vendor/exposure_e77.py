# ruff: file-ignore[line-too-long]
"""Epic 77 (spec A3; build plan §4 exposure-file rule): the ``portal_vendor``-surface exposures of the ``portal_vendor.*``
tools. Loaded by the package's ``hooks.py`` (``agent_tools._loader``); ``validate_exposures`` refuses a tool without the
``portal_`` key prefix on a portal surface. Portal tools are never MCP-exposed (Epic 73)."""

from app.core.agentic.exposure import ToolExposure, expose_tool, exposures
from app.services.agent_tools.portal_vendor.tools_e77 import TOOLS

for _tool in TOOLS:
    if _tool.key not in exposures():
        expose_tool(
            ToolExposure(
                _tool.key, frozenset({"portal_vendor"}), portal_permissions=_tool.permissions, max_results=20
            )
        )  # type: ignore[arg-type]
