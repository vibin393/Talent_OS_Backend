# ruff: noqa: E501
"""Authorisation responses and the per-token gate cache (Epic 73, spec 0.2; FR-376). Pure.

Gate order (performed by the facade): bearer (401) -> tenant MCP enabled (401) / ``agent_mcp`` + ``agentic`` + no AI
kill switch (403) -> ecosystem suspension (403) -> the user's ``mcp:use`` and no open hold (403) -> an ``mcp:*`` scope
(403 ``insufficient_scope``). Results are cached per token for ``mcp_gate_cache_seconds`` (15 s), so every effect
lands within 1 minute (FR-380)."""

import dataclasses
import time
from typing import Any

from app.core.settings import get_settings

OFF_TEXT = "AI assistant access is turned off for this organisation"
PAUSED_TEXT = "AI tools are paused by your organisation"
NO_PERMISSION_TEXT = "You don't have access to AI assistant tools"
HOLD_TEXT = "AI assistant access is paused for your account; contact your AI Administrator"
SUSPENDED_TEXT = "External access to this workspace is suspended"
SCOPE_TEXT = "This connection needs an AI assistant (mcp) permission"


def metadata_url() -> str:
    return f"{get_settings().p5_mcp_resource_url().rsplit('/mcp', 1)[0]}/.well-known/oauth-protected-resource/mcp"


def www_authenticate(*, error: str | None = None, description: str | None = None, scope: str = "mcp:read") -> str:
    parts = [f'resource_metadata="{metadata_url()}"']
    if error:
        parts.insert(0, f'error="{error}"')
    if description:
        parts.append(f'error_description="{description}"')
    parts.append(f'scope="{scope}"')
    return "Bearer " + ", ".join(parts)


class GateRefusal(Exception):  # noqa: N818 - protocol name
    """An HTTP-level refusal with a JSON-RPC error body (``-32001``)."""

    def __init__(self, status: int, code: str, message: str, *, www: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.www = www

    def headers(self) -> dict[str, str]:
        return {"WWW-Authenticate": self.www} if self.www else {}


def unauthorized(code: str = "invalid_token", message: str = "Authentication required", *, error: str | None = None) -> GateRefusal:
    return GateRefusal(401, code, message, www=www_authenticate(error=error))


def forbidden(code: str, message: str) -> GateRefusal:
    return GateRefusal(403, code, message)


def insufficient_scope() -> GateRefusal:
    return GateRefusal(403, "insufficient_scope", SCOPE_TEXT, www=www_authenticate(error="insufficient_scope"))


class GateCache:
    """``token_id -> (value, expiry)``; the value is a refusal or the passed gates (per process)."""

    def __init__(self) -> None:
        self._data: dict[Any, tuple[Any, float]] = {}

    def get(self, key: Any) -> Any:
        hit = self._data.get(key)
        if hit is None or hit[1] <= time.monotonic():
            self._data.pop(key, None)
            return None
        return hit[0]

    def put(self, key: Any, value: Any) -> None:
        ttl = max(0, int(get_settings().mcp_gate_cache_seconds))
        if ttl:
            self._data[key] = (value, time.monotonic() + ttl)

    def clear(self) -> None:
        self._data.clear()


gate_cache = GateCache()


@dataclasses.dataclass(frozen=True, slots=True)
class PassedGates:
    tenant_id: Any
    user_id: Any


__all__ = [
    "HOLD_TEXT",
    "NO_PERMISSION_TEXT",
    "OFF_TEXT",
    "PAUSED_TEXT",
    "SCOPE_TEXT",
    "SUSPENDED_TEXT",
    "GateCache",
    "GateRefusal",
    "PassedGates",
    "forbidden",
    "gate_cache",
    "insufficient_scope",
    "metadata_url",
    "unauthorized",
    "www_authenticate",
]
