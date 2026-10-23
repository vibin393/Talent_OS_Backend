# ruff: file-ignore[line-too-long]
"""Epic 73 registrations, autodiscovered by ``core.hooks``: the session-ending event handlers (kill switches, MCP
disable, uninstall, revocation -> task ``mcp.end_sessions``), the notification types, the settings-hub entry and the
audit labels. A registration-only edge into the facade (``.importlinter`` ignore)."""

from app.services import mcp_service

mcp_service.register_hooks()
