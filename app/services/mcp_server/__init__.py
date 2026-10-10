# ruff: noqa: E501
"""Governed MCP Server (Epic 73, AD-84, FR-375..FR-380): protocol code only -- no SQL, no tools, no runtime internals
(contract ``p5-mcp-via-runtime``). ``transport.handle(request, backend)`` runs one HTTP exchange against a
``ports.McpBackend``, which the ``mcp_service`` facade implements."""
