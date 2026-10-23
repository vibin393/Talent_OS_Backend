# ruff: file-ignore[line-too-long]
"""Epic 70 (spec A3): MCP exposures of the job board tools. Publishing through MCP is always a Proposal
(``mcp:propose``); no internal AgentSpec lists ``job_boards.publish`` (agents never publish, P4 FR-281 card)."""

from app.core.agentic.exposure import ToolExposure, expose_tool, exposures

if "job_boards.publish" not in exposures():
    expose_tool(ToolExposure("job_boards.publish", frozenset({"mcp"}), "mcp:propose", ("postings:write",), "postings"))
if "job_boards.status" not in exposures():
    expose_tool(ToolExposure("job_boards.status", frozenset({"mcp"}), "mcp:read", ("postings:read",), "postings"))
