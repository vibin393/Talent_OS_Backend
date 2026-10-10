# ruff: noqa: E501
"""Scripted MCP test client (Epic 73, spec A7; reused by Epic 74's MCP test client and the docs examples).

Speaks the stateless Streamable-HTTP JSON transport to ``/api/mcp`` over the ASGI app (no network): ``initialize`` /
``tools/list`` / ``tools/call`` / ``resources/*`` / ``DELETE``, asserting the JSON-RPC envelope on every answer.
Tokens come from ``tests.support.oauth.mint(..., resource="mcp")``.
"""

import itertools
import json
from typing import Any

import httpx

DEFAULT_VERSION = "2025-11-25"
PATH = "/api/mcp"


class McpClient:
    def __init__(self, app: Any, token: str | None, *, version: str = DEFAULT_VERSION, origin: str | None = None) -> None:
        self.app = app
        self.token = token
        self.version = version
        self.origin = origin
        self.session_id: str | None = None
        self.negotiated: str | None = None
        self._ids = itertools.count(1)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def aclose(self) -> None:
        await self.http.aclose()

    def headers(self, *, session: bool = True, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.origin:
            h["Origin"] = self.origin
        if session and self.session_id:
            h["Mcp-Session-Id"] = self.session_id
            h["MCP-Protocol-Version"] = self.negotiated or self.version
        h.update(extra or {})
        return h

    async def post(self, payload: Any, *, session: bool = True, headers: dict[str, str] | None = None, raw: bytes | None = None) -> httpx.Response:
        body = raw if raw is not None else json.dumps(payload).encode()
        return await self.http.post(PATH, content=body, headers=self.headers(session=session, extra=headers))

    @staticmethod
    def envelope(resp: httpx.Response) -> dict[str, Any]:
        data = resp.json()
        assert data.get("jsonrpc") == "2.0", data
        assert ("result" in data) != ("error" in data), data
        return data

    async def initialize(self, *, version: str | None = None, client: str = "Claude", expect_ok: bool = True) -> httpx.Response:
        resp = await self.post(
            {
                "jsonrpc": "2.0", "id": next(self._ids), "method": "initialize",
                "params": {"protocolVersion": version or self.version, "capabilities": {}, "clientInfo": {"name": client, "version": "1.0"}},
            },
            session=False,
        )  # fmt: skip
        if expect_ok:
            assert resp.status_code == 200, resp.text
            data = self.envelope(resp)
            assert "result" in data, data
            self.session_id = resp.headers["Mcp-Session-Id"]
            self.negotiated = data["result"]["protocolVersion"]
            notified = await self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            assert notified.status_code == 202, notified.text
        return resp

    async def rpc(self, method: str, params: dict[str, Any] | None = None, *, request_id: Any = None) -> httpx.Response:
        rid = request_id if request_id is not None else next(self._ids)
        return await self.post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})

    async def result(self, method: str, params: dict[str, Any] | None = None, *, request_id: Any = None) -> dict[str, Any]:
        resp = await self.rpc(method, params, request_id=request_id)
        assert resp.status_code == 200, resp.text
        data = self.envelope(resp)
        assert "result" in data, data
        return data["result"]

    async def list_tools(self) -> list[dict[str, Any]]:
        return (await self.result("tools/list"))["tools"]

    async def call(self, name: str, arguments: dict[str, Any] | None = None, *, request_id: Any = None) -> dict[str, Any]:
        out = await self.result("tools/call", {"name": name, "arguments": arguments or {}}, request_id=request_id)
        assert isinstance(out.get("content"), list) and "isError" in out, out
        return out

    async def delete(self) -> httpx.Response:
        return await self.http.delete(PATH, headers=self.headers())


__all__ = ["DEFAULT_VERSION", "PATH", "McpClient"]
