# ruff: noqa: E501
"""Stateless Streamable-HTTP transport for MCP (Epic 73, spec 0.1; AD-84, D-23). Hand-rolled, no SDK dependency.

* ``POST /api/mcp``: one JSON-RPC message; the answer is always ``application/json`` (never a stream). Notifications and
  client responses -> 202 with no body.
* ``GET`` -> 405 (``Allow: POST, DELETE``): no server-initiated stream, notifications or sampling.
* ``DELETE`` with ``Mcp-Session-Id`` -> ends the session (``client_closed``), 204.
* Order: bearer + gates (401 / 403) -> Origin allow-list (403) -> ``Accept`` (406) -> body cap (413) -> JSON-RPC parse
  (400) -> ``initialize`` (version negotiation, ``Mcp-Session-Id``) or the session (400 missing, 404 unknown / ended /
  paused, 400 wrong ``MCP-Protocol-Version``) -> the method.
* No cookie is ever read: the bearer token is the only credential.
"""

import json
from typing import Any

from loguru import logger
from starlette.requests import Request
from starlette.responses import Response

from app.core.settings import get_settings
from app.services.mcp_server import protocol
from app.services.mcp_server.auth import GateRefusal, unauthorized
from app.services.mcp_server.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    SERVER_ERROR,
    Message,
    RpcError,
    error,
    parse,
    result,
)
from app.services.mcp_server.ports import Caller, McpBackend, SessionGone

SESSION_HEADER = "Mcp-Session-Id"
VERSION_HEADER = "MCP-Protocol-Version"
NO_STORE = {"Cache-Control": "no-store"}
_ACCEPTABLE = ("application/json", "text/event-stream", "application/*", "*/*")


def _json(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(
        json.dumps(body, ensure_ascii=False, default=str, separators=(",", ":")),
        status_code=status,
        media_type="application/json",
        headers={**NO_STORE, **(headers or {})},
    )


def _rpc_error(msg_id: Any, code: int, message: str, data: Any = None, *, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return _json(error(msg_id, code, message, data), status, headers)


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header[:7].lower() != "bearer ":
        return None
    token = header[7:].strip()
    return token or None


def _accept_ok(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    if not accept:
        return False
    media = {part.split(";", 1)[0].strip() for part in accept.split(",")}
    return any(m in media for m in _ACCEPTABLE)


def _cors(origin: str | None) -> dict[str, str]:
    if not origin:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Expose-Headers": f"{SESSION_HEADER}, WWW-Authenticate",
        "Vary": "Origin",
    }


async def handle(request: Request, backend: McpBackend) -> Response:
    if request.method not in ("POST", "DELETE"):
        return Response(status_code=405, headers={"Allow": "POST, DELETE", **NO_STORE})
    token = _bearer(request)
    try:
        if token is None:
            raise unauthorized("invalid_token", "Authentication required")
        caller = await backend.authenticate(request, token)
    except GateRefusal as refusal:
        return _rpc_error(None, SERVER_ERROR, refusal.message, {"code": refusal.code}, status=refusal.status, headers=refusal.headers())
    origin = request.headers.get("origin")
    if origin and not await backend.origin_allowed(caller, origin):
        await backend.audit_refusal(caller, "mcp.origin_refused", {"origin": origin[:200]})
        return _rpc_error(None, SERVER_ERROR, "This origin is not allowed to use AI assistant tools", {"code": "origin_not_allowed"}, status=403)
    cors = _cors(origin)
    if request.method == "DELETE":
        return await _delete(request, backend, caller, cors)
    if not _accept_ok(request):
        return _rpc_error(None, INVALID_REQUEST, "Accept must include application/json or text/event-stream", status=406, headers=cors)
    limit = get_settings().mcp_max_request_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return _rpc_error(None, INVALID_REQUEST, "Request body too large", {"max_bytes": limit}, status=413, headers=cors)
    body = await request.body()
    if len(body) > limit:
        return _rpc_error(None, INVALID_REQUEST, "Request body too large", {"max_bytes": limit}, status=413, headers=cors)
    try:
        msg = parse(body)
    except RpcError as exc:
        return _rpc_error(None, exc.code, exc.message, exc.data, status=exc.http_status, headers=cors)
    if msg.kind != "request":
        return Response(status_code=202, headers={**NO_STORE, **cors})
    if msg.method == "initialize":
        return await _initialize(msg, backend, caller, cors)
    return await _request(request, msg, backend, caller, cors)


async def _initialize(msg: Message, backend: McpBackend, caller: Caller, cors: dict[str, str]) -> Response:
    try:
        version = protocol.negotiate(msg.params)
    except RpcError as exc:
        await backend.audit_refusal(caller, "mcp.protocol_refused", {"requested": str(msg.params.get("protocolVersion"))[:40]})
        return _rpc_error(msg.id, exc.code, exc.message, exc.data, headers=cors)
    info = msg.params.get("clientInfo")
    info = info if isinstance(info, dict) else {}
    name = info.get("title") or info.get("name")
    client_version = info.get("version")
    try:
        session_id, _session = await backend.open_session(
            caller,
            protocol_version=version,
            client_name=str(name) if isinstance(name, str) and name.strip() else caller.app_name,
            client_version=str(client_version)[:40] if isinstance(client_version, str | int) else None,
        )
    except GateRefusal as refusal:
        return _rpc_error(msg.id, SERVER_ERROR, refusal.message, {"code": refusal.code}, status=refusal.status, headers={**refusal.headers(), **cors})
    return _json(result(msg.id, protocol.initialize_result(version)), headers={SESSION_HEADER: session_id, **cors})


async def _request(request: Request, msg: Message, backend: McpBackend, caller: Caller, cors: dict[str, str]) -> Response:
    session_id = request.headers.get(SESSION_HEADER)
    if not session_id:
        return _rpc_error(msg.id, INVALID_REQUEST, f"Missing {SESSION_HEADER} header; call initialize first", status=400, headers=cors)
    try:
        session = await backend.find_session(caller, session_id)
    except SessionGone as gone:
        return _rpc_error(msg.id, SERVER_ERROR, f"Session ended: {gone.reason}", {"code": "session_ended"}, status=404, headers=cors)
    if request.headers.get(VERSION_HEADER) != session.protocol_version:
        return _rpc_error(
            msg.id, INVALID_REQUEST, f"{VERSION_HEADER} must be {session.protocol_version}", status=400, headers=cors
        )
    try:
        value = await protocol.dispatch(
            msg.method or "", msg.params, caller=caller, session=session, backend=backend, request_id=msg.id
        )
    except RpcError as exc:
        return _rpc_error(msg.id, exc.code, exc.message, exc.data, headers=cors)
    except GateRefusal as refusal:
        return _rpc_error(msg.id, SERVER_ERROR, refusal.message, {"code": refusal.code}, status=refusal.status, headers={**refusal.headers(), **cors})
    except SessionGone as gone:
        return _rpc_error(msg.id, SERVER_ERROR, f"Session ended: {gone.reason}", {"code": "session_ended"}, status=404, headers=cors)
    except Exception:
        logger.exception("MCP {} failed", msg.method)
        return _rpc_error(msg.id, INTERNAL_ERROR, "Internal error", headers=cors)
    return _json(result(msg.id, value), headers=cors)


async def _delete(request: Request, backend: McpBackend, caller: Caller, cors: dict[str, str]) -> Response:
    session_id = request.headers.get(SESSION_HEADER)
    if not session_id:
        return _rpc_error(None, INVALID_REQUEST, f"Missing {SESSION_HEADER} header", status=400, headers=cors)
    try:
        session = await backend.find_session(caller, session_id)
    except SessionGone as gone:
        return _rpc_error(None, SERVER_ERROR, f"Session ended: {gone.reason}", {"code": "session_ended"}, status=404, headers=cors)
    await backend.end_session(caller, session, reason="client_closed")
    return Response(status_code=204, headers={**NO_STORE, **cors})


__all__ = ["SESSION_HEADER", "VERSION_HEADER", "handle"]
