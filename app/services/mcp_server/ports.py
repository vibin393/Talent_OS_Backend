# ruff: noqa: E501
"""The MCP server's port (Epic 73): the protocol code in ``mcp_server`` never touches the database, OAuth, the agent
runtime or a domain; the ``mcp_service`` facade implements this protocol and passes itself to ``transport.handle``
(contract ``p5-mcp-via-runtime``)."""

import dataclasses
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from starlette.requests import Request


@dataclasses.dataclass(frozen=True, slots=True)
class Caller:
    """The authenticated, gated delegated token (``app`` Principal acting for ``user_id``)."""

    tenant_id: uuid.UUID
    installation_id: uuid.UUID
    user_id: uuid.UUID
    app_id: uuid.UUID | None
    token_id: uuid.UUID | None
    scopes: frozenset[str]
    permissions: Mapping[str, Any]
    app_name: str
    sandbox: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class SessionRef:
    id: uuid.UUID
    run_id: uuid.UUID
    protocol_version: str
    installation_id: uuid.UUID
    user_id: uuid.UUID
    client_name: str
    key: str  # hex SHA-256 of the session id (limits / detector key; never the raw id)


class SessionGone(Exception):  # noqa: N818 - protocol name
    """Unknown, ended or paused session (HTTP 404: the client must re-initialize)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class McpBackend(Protocol):
    async def authenticate(self, request: Request, token: str | None) -> Caller: ...

    async def origin_allowed(self, caller: Caller, origin: str) -> bool: ...

    async def open_session(
        self, caller: Caller, *, protocol_version: str, client_name: str, client_version: str | None
    ) -> tuple[str, SessionRef]: ...

    async def find_session(self, caller: Caller, session_id: str) -> SessionRef: ...

    async def list_tools(self, caller: Caller, session: SessionRef) -> dict[str, Any]: ...

    async def call_tool(
        self, caller: Caller, session: SessionRef, *, name: str, arguments: dict[str, Any], request_id: str | int | None
    ) -> dict[str, Any]: ...

    async def end_session(self, caller: Caller, session: SessionRef, *, reason: str) -> None: ...

    async def audit_refusal(self, caller: Caller | None, action: str, detail: Mapping[str, Any]) -> None: ...


__all__ = ["Caller", "McpBackend", "SessionGone", "SessionRef"]
