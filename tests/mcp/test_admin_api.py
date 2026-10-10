# ruff: noqa: E501
"""Epic 73 (spec A3/A4): enable needs the notice ack and AI agents; mutations are human only; low-tool modes only for
the creating tools; sessions own vs all; holds; cross-tenant 404."""

import uuid

import pytest
from fastapi import FastAPI

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, Forbidden
from app.core.permissions import Principal
from app.schemas.mcp import McpEnableIn
from app.services import ai, mcp_service
from tests.agents.conftest import AgentOrg
from tests.mcp.conftest import McpEnv, make_env


async def test_settings_enable_disable_over_http(app: FastAPI, agent_org: AgentOrg) -> None:
    admin = agent_org.admin
    resp = await admin.client.get("/api/v1/mcp/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False and body["current_notice_version"] == "mcp-notice-v1"
    assert [g["key"] for g in body["groups"]][:2] == ["core", "recruiting"] and body["tier_calls_per_minute"] == 60
    no_ack = await admin.client.post("/api/v1/mcp/enable", json={"notice_version": "mcp-notice-v1", "acknowledged": False, "tool_groups": ["core"]}, headers=admin.h())
    assert no_ack.status_code == 422
    ok = await admin.client.post("/api/v1/mcp/enable", json={"notice_version": "mcp-notice-v1", "acknowledged": True, "tool_groups": ["core", "recruiting"]}, headers=admin.h())
    assert ok.status_code == 200, ok.text
    assert ok.json()["enabled"] is True and ok.json()["tool_groups"] == ["core", "recruiting"]
    patch = await admin.client.patch("/api/v1/mcp/settings", json={"low_tool_modes": {"messaging.send": "auto"}}, headers=admin.h())
    assert patch.status_code == 422
    patch = await admin.client.patch("/api/v1/mcp/settings", json={"calls_per_minute": 30, "allowed_origins": ["https://a.example"]}, headers=admin.h())
    assert patch.status_code == 200 and patch.json()["calls_per_minute"] == 30
    tools = await admin.client.get("/api/v1/mcp/tools")
    assert tools.status_code == 200 and {t["execution"] for t in tools.json()} == {"read", "draft", "propose"}
    me = await admin.client.get("/api/v1/mcp/me")
    assert me.json() == {"enabled": True, "has_permission": True, "on_hold": False, "server_url": me.json()["server_url"]}
    off = await admin.client.post("/api/v1/mcp/disable", json={"reason": "Pause"}, headers=admin.h())
    assert off.status_code == 200 and off.json()["enabled"] is False and off.json()["tool_groups"] == ["core", "recruiting"]


async def test_enable_needs_ai_agents_and_a_person(agent_org: AgentOrg) -> None:
    async with tenant_session(agent_org.tenant_id) as db:
        await ai.set_features(db, agent_org.tenant_id, {"agentic": False})
    data = McpEnableIn(notice_version="mcp-notice-v1", acknowledged=True, tool_groups=["core"])
    async with tenant_session(agent_org.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await mcp_service.enable(agent_org.ctx(), db, data)
        assert exc.value.code == "agentic_not_enabled"
    for kind in ("app", "api_key"):
        ctx = RequestContext(tenant_id=agent_org.tenant_id, principal=Principal(type=kind, id=uuid.uuid4(), tenant_id=agent_org.tenant_id))  # type: ignore[arg-type]
        async with tenant_session(agent_org.tenant_id) as db:
            with pytest.raises(Forbidden):
                await mcp_service.enable(ctx, db, data)


async def test_sessions_own_vs_all_and_cross_tenant(mcp_env: McpEnv, app: FastAPI, other_org: AgentOrg) -> None:
    await mcp_env.client()
    admin = mcp_env.org.admin
    page = (await admin.client.get("/api/v1/mcp/sessions")).json()
    assert page["total"] == 1 and page["items"][0]["status"] == "active"
    sid = page["items"][0]["id"]
    other = await make_env(app, other_org)
    stranger = other.org.admin
    assert (await stranger.client.post(f"/api/v1/mcp/sessions/{sid}/end", json={}, headers=stranger.h())).status_code == 404
    assert (await stranger.client.get("/api/v1/mcp/sessions")).json()["total"] == 0
    ended = await admin.client.post(f"/api/v1/mcp/sessions/{sid}/end", json={"reason": "test"}, headers=admin.h())
    assert ended.status_code == 200 and ended.json()["status"] == "ended" and ended.json()["end_reason"] == "admin"
    holds = await admin.client.get("/api/v1/mcp/holds")
    assert holds.status_code == 200 and holds.json() == []
    assert (await admin.client.post(f"/api/v1/mcp/holds/{uuid.uuid4()}/release", json={"note": "x"}, headers=admin.h())).status_code == 404


async def test_mcp_token_cannot_use_the_admin_api(mcp_env: McpEnv) -> None:
    c = await mcp_env.client(init=False)
    resp = await c.http.get("/api/v1/mcp/settings", headers={"Authorization": f"Bearer {c.token}"})
    assert resp.status_code in (401, 403)
