# ruff: noqa: E501
"""JSON-RPC 2.0 envelope for the MCP endpoint (Epic 73, spec 0.1 / 0.5). Pure.

One message per POST: a batch (JSON array) is ``-32600`` (batching was removed in protocol 2025-06-18)."""

import dataclasses
import json
from typing import Any, Literal

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SERVER_ERROR = -32001  # Talent OS: access / session refusals

MessageKind = Literal["request", "notification", "response"]


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None, *, http_status: int = 200) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
        self.http_status = http_status


@dataclasses.dataclass(frozen=True, slots=True)
class Message:
    kind: MessageKind
    id: str | int | None
    method: str | None
    params: dict[str, Any]


def parse(body: bytes) -> Message:
    """Parse one JSON-RPC message; raises ``RpcError`` (HTTP 400) for malformed JSON, batches and bad envelopes."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RpcError(PARSE_ERROR, "Parse error", http_status=400) from exc
    if isinstance(data, list):
        raise RpcError(INVALID_REQUEST, "Batch requests are not supported", http_status=400)
    if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
        raise RpcError(INVALID_REQUEST, "Invalid Request", http_status=400)
    msg_id = data.get("id")
    if msg_id is not None and (isinstance(msg_id, bool) or not isinstance(msg_id, str | int)):
        raise RpcError(INVALID_REQUEST, "Invalid Request: id must be a string or an integer", http_status=400)
    method = data.get("method")
    if method is None:
        if "result" in data or "error" in data:
            return Message("response", msg_id, None, {})
        raise RpcError(INVALID_REQUEST, "Invalid Request", http_status=400)
    if not isinstance(method, str):
        raise RpcError(INVALID_REQUEST, "Invalid Request: method must be a string", http_status=400)
    params = data.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "Invalid params: params must be an object", http_status=400)
    if "id" not in data:
        return Message("notification", None, method, params)
    return Message("request", msg_id, method, params)


def result(msg_id: str | int | None, value: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": value}


def error(msg_id: str | int | None, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "SERVER_ERROR",
    "Message",
    "RpcError",
    "error",
    "parse",
    "result",
]
