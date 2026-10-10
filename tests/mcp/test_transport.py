# ruff: noqa: E501
"""Epic 73 (spec 0.1, D-23): protocol conformance of the hand-rolled stateless Streamable-HTTP transport.

Methods and statuses (405 / 406 / 413 / 400 / 404 / 202 / 204), batching refused, notifications and responses 202,
``MCP-Protocol-Version`` enforcement, the Origin allow-list, ``DELETE`` ends the session, both pinned versions
negotiate and any other version is refused at initialisation."""

import json

import pytest
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.agent_runtime import AgentRun
from app.models.mcp import McpSession
from app.services.mcp_server import protocol
from tests.mcp.conftest import McpEnv
from tests.support.mcp_client import McpClient


async def test_get_is_405_and_unknown_methods(mcp_env: McpEnv) -> None:
    c = await mcp_env.client(init=False)
    resp = await c.http.get("/api/mcp", headers=c.headers())
    assert resp.status_code == 405 and resp.headers["allow"] == "POST, DELETE"


@pytest.mark.parametrize("version", ["2025-11-25", "2025-06-18"])
async def test_initialize_negotiates_pinned_versions(mcp_env: McpEnv, version: str) -> None:
    c = await mcp_env.client(init=False, version=version)
    resp = await c.initialize()
    data = resp.json()["result"]
    assert data["protocolVersion"] == version
    assert data["capabilities"] == {"tools": {"listChanged": False}, "resources": {"listChanged": False}}
    assert data["serverInfo"]["name"] == "talent-os"
    assert data["instructions"] == protocol.INSTRUCTIONS
    assert c.session_id and c.session_id.startswith("mcps_")
    assert resp.headers["cache-control"] == "no-store"
    async with tenant_session(mcp_env.org.tenant_id) as db:
        row = (await db.execute(select(McpSession))).scalar_one()
        assert row.session_hash != c.session_id.encode() and len(row.session_hash) == 32
        assert row.protocol_version == version and row.client_name == "Claude" and row.status == "active"
        run = await db.get(AgentRun, row.run_id)
        assert run is not None and run.origin == "external" and run.status == "running"
    assert await c.result("ping") == {}


async def test_unsupported_version_is_refused_without_session(mcp_env: McpEnv) -> None:
    c = await mcp_env.client(init=False)
    resp = await c.initialize(version="2024-11-05", expect_ok=False)
    body = resp.json()
    assert body["error"]["code"] == -32602
    assert body["error"]["data"] == {"supported": ["2025-11-25", "2025-06-18"], "requested": "2024-11-05"}
    assert "mcp-session-id" not in resp.headers
    async with tenant_session(mcp_env.org.tenant_id) as db:
        assert (await db.execute(select(McpSession))).first() is None


async def test_envelope_rules(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    batch = await c.post([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert batch.status_code == 400 and batch.json()["error"]["code"] == -32600
    bad = await c.post(None, raw=b"{not json")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == -32700
    note = await c.post({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
    assert note.status_code == 202 and note.content == b""
    response = await c.post({"jsonrpc": "2.0", "id": 9, "result": {}})
    assert response.status_code == 202
    unknown = await c.rpc("prompts/list")
    assert unknown.json()["error"]["code"] == -32601
    for method in ("completion/complete", "logging/setLevel", "sampling/createMessage"):
        assert (await c.rpc(method)).json()["error"]["code"] == -32601
    big = await c.post(None, raw=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"x": "a" * 300_000}}).encode())
    assert big.status_code == 413
    no_accept = await c.post({"jsonrpc": "2.0", "id": 2, "method": "ping"}, headers={"Accept": "text/html"})
    assert no_accept.status_code == 406


async def test_session_header_rules(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    missing = await c.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, session=False)
    assert missing.status_code == 400
    wrong_version = await c.post({"jsonrpc": "2.0", "id": 2, "method": "ping"}, headers={"MCP-Protocol-Version": "2025-06-18"})
    assert wrong_version.status_code == 400
    unknown = await c.post({"jsonrpc": "2.0", "id": 3, "method": "ping"}, headers={"Mcp-Session-Id": "mcps_nope"})
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == -32001
    gone = await c.delete()
    assert gone.status_code == 204
    after = await c.rpc("ping")
    assert after.status_code == 404 and "client_closed" in after.json()["error"]["message"]
    async with tenant_session(mcp_env.org.tenant_id) as db:
        row = (await db.execute(select(McpSession))).scalar_one()
        assert (row.status, row.end_reason) == ("ended", "client_closed") and row.ended_at is not None
        run = await db.get(AgentRun, row.run_id)
        assert run is not None and run.status != "running"


async def test_origin_allow_list(mcp_env: McpEnv) -> None:
    c = await mcp_env.client(init=False, origin="https://evil.example")
    resp = await c.initialize(expect_ok=False)
    assert resp.status_code == 403
    from app.schemas.mcp import McpSettingsIn
    from app.services import mcp_service
    from app.services.mcp_server import auth as mcp_auth

    async with tenant_session(mcp_env.org.tenant_id) as db:
        await mcp_service.update_settings(mcp_env.human_ctx(), db, McpSettingsIn(allowed_origins=["https://assistant.example"]))
    mcp_auth.gate_cache.clear()
    ok = McpClient(mcp_env.app, c.token, origin="https://assistant.example")
    mcp_env.clients.append(ok)
    resp = await ok.initialize()
    assert resp.headers["access-control-allow-origin"] == "https://assistant.example"


async def test_session_bound_to_token_user_and_installation(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    other_app = await __import__("tests.support.oauth", fromlist=["make_app"]).make_app(name="Other", scopes=("mcp:read",), grant_types=("authorization_code",), resources=("mcp",))
    from tests.support.oauth import install_app, mint

    inst = await install_app(mcp_env.org.tenant_id, other_app, granted_by=mcp_env.org.admin.user_id)
    token = (await mint(inst, mcp_env.org.admin.user_id, scopes=("mcp:read",), resource="mcp")).access_token
    stranger = McpClient(mcp_env.app, token)
    mcp_env.clients.append(stranger)
    stranger.session_id, stranger.negotiated = c.session_id, c.negotiated
    assert (await stranger.rpc("ping")).status_code == 404
    # a refreshed token of the same authorisation continues the session
    same = McpClient(mcp_env.app, await mcp_env.token())
    mcp_env.clients.append(same)
    same.session_id, same.negotiated = c.session_id, c.negotiated
    assert await same.result("ping") == {}


async def test_resources(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    listed = await c.result("resources/list")
    uris = [r["uri"] for r in listed["resources"]]
    assert uris == ["talentos://help/getting-started", "talentos://help/proposals", "talentos://help/limits"]
    read = await c.result("resources/read", {"uri": "talentos://help/proposals"})
    assert read["contents"][0]["mimeType"] == "text/markdown" and "nothing has happened yet" in read["contents"][0]["text"]
    assert await c.result("resources/templates/list") == {"resourceTemplates": []}
    bad = await c.rpc("resources/read", {"uri": "file:///etc/passwd"})
    assert bad.json()["error"]["code"] == -32602
