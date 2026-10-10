# ruff: noqa: E501
"""Epic 73 (spec 0.2, FR-376): bearer, enablement, kill switches, ``mcp:use``, holds, scopes and the RFC 9728 document."""

from fastapi import FastAPI

from app.core.database import tenant_session
from app.services import agent_runtime_service, ecosystem_service
from app.services.mcp_server import auth as mcp_auth
from app.services.mcp_store import holds
from tests.agents.conftest import AgentOrg
from tests.mcp.conftest import McpEnv, make_env
from tests.modules.identity.conftest import join
from tests.support.mcp_client import McpClient


def _www(resp: object) -> str:
    return resp.headers.get("www-authenticate", "")  # type: ignore[attr-defined]


async def test_no_token_401_with_resource_metadata(mcp_env: McpEnv) -> None:
    c = McpClient(mcp_env.app, None)
    mcp_env.clients.append(c)
    resp = await c.initialize(expect_ok=False)
    assert resp.status_code == 401
    assert 'resource_metadata="' in _www(resp) and "/.well-known/oauth-protected-resource/mcp" in _www(resp)
    assert resp.json()["error"]["code"] == -32001
    cookie_only = await c.http.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Accept": "application/json"}, cookies={"session": "x"})
    assert cookie_only.status_code == 401


async def test_before_enablement_every_request_is_an_auth_error(app: FastAPI, agent_org: AgentOrg) -> None:
    env = await make_env(app, agent_org, enabled=False)
    c = await env.client(init=False)
    resp = await c.initialize(expect_ok=False)
    assert resp.status_code == 401 and "resource_metadata" in _www(resp)
    assert resp.json()["error"]["message"] == mcp_auth.OFF_TEXT
    await c.aclose()


async def test_api_resource_token_is_refused(mcp_env: McpEnv) -> None:
    token = await mcp_env.token(scopes=("candidates:read",), resource="api")
    c = McpClient(mcp_env.app, token)
    mcp_env.clients.append(c)
    assert (await c.initialize(expect_ok=False)).status_code == 401


async def test_user_without_mcp_use_is_refused(mcp_env: McpEnv) -> None:
    from app.models.mcp import McpSession  # noqa: F401

    tenant = mcp_env.org.tenant
    viewer = await join(mcp_env.app, tenant, "viewer", name="Vera Viewer")
    c = await mcp_env.client(scopes=("mcp:read", "candidates:read"), user=viewer.user_id, init=False)
    resp = await c.initialize(expect_ok=False)
    assert resp.status_code == 403 and resp.json()["error"]["message"] == mcp_auth.NO_PERMISSION_TEXT
    await viewer.client.aclose()


async def test_hold_and_kill_switches_refuse(mcp_env: McpEnv) -> None:
    org = mcp_env.org
    async with tenant_session(org.tenant_id) as db:
        await holds.open_(db, tenant_id=org.tenant_id, user_id=org.admin.user_id, installation_id=None, session_id=None, reason="admin", auto_release_hours=0)
    c = await mcp_env.client(init=False)
    resp = await c.initialize(expect_ok=False)
    assert resp.status_code == 403 and resp.json()["error"]["message"] == mcp_auth.HOLD_TEXT
    async with tenant_session(org.tenant_id) as db:
        hold = await holds.active_for(db, org.admin.user_id)
        assert hold is not None
        await holds.release(db, hold, by=org.admin.user_id, note="ok")
    mcp_auth.gate_cache.clear()
    ok = await mcp_env.client()
    assert await ok.result("ping") == {}
    # AI kill switch -> 403 on the next request (gates re-evaluated; the cache is cleared by the event handler)
    async with tenant_session(org.tenant_id) as db:
        await agent_runtime_service.set_kill_switch(mcp_env.human_ctx(), db, on=True, reason="test")
    mcp_auth.gate_cache.clear()
    assert (await ok.rpc("ping")).status_code == 403
    async with tenant_session(org.tenant_id) as db:
        await agent_runtime_service.set_kill_switch(mcp_env.human_ctx(), db, on=False, reason=None)
    mcp_auth.gate_cache.clear()
    # Ecosystem kill switch -> 403 ecosystem_suspended
    async with tenant_session(org.tenant_id) as db:
        await ecosystem_service.set_kill_switch(mcp_env.human_ctx(), db, on=True, reason="test")
    ecosystem_service.bust_cache()
    mcp_auth.gate_cache.clear()
    from app.services import oauth_service

    oauth_service.clear_cache()
    resp = await ok.rpc("ping")
    assert resp.status_code == 403
    async with tenant_session(org.tenant_id) as db:
        await ecosystem_service.set_kill_switch(mcp_env.human_ctx(), db, on=False, reason="done")
    ecosystem_service.bust_cache()


def test_insufficient_scope_header() -> None:
    refusal = mcp_auth.insufficient_scope()
    assert refusal.status == 403
    assert refusal.headers()["WWW-Authenticate"].startswith('Bearer error="insufficient_scope"')
    assert 'scope="mcp:read"' in refusal.headers()["WWW-Authenticate"]


async def test_protected_resource_metadata(mcp_env: McpEnv) -> None:
    c = McpClient(mcp_env.app, None)
    mcp_env.clients.append(c)
    for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
        resp = await c.http.get(path)
        assert resp.status_code == 200, path
        doc = resp.json()
        assert doc["resource"].endswith("/mcp")
        assert doc["scopes_supported"] == ["mcp:read", "mcp:draft", "mcp:propose", "offline_access"]
        assert doc["bearer_methods_supported"] == ["header"] and doc["resource_name"] == "Talent OS"
        assert len(doc["authorization_servers"]) == 1
        assert "max-age=3600" in resp.headers["cache-control"]
