# ruff: noqa: E501, E741
"""Epic 73 (spec 0.7, FR-380): every kill switch, disablement, uninstall and revocation ends sessions (task
``mcp.end_sessions``); the gates refuse the next request even without the task; idle runs are mirrored."""

import uuid

import pytest
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.events.agents import KillSwitchChanged
from app.events.ecosystem import (
    EcosystemKillSwitchChanged,
    InstallationChanged,
    OAuthTokenFamilyRevoked,
    UserGrantChanged,
)
from app.events.mcp import McpEnablementChanged
from app.models.agent_runtime import AgentRun
from app.models.mcp import McpSession
from app.services import agent_runtime_service, mcp_service
from app.services.mcp_server import auth as mcp_auth
from app.services.mcp_server import lifecycle
from tests.mcp.conftest import McpEnv

T, I, U, A = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (KillSwitchChanged(T, "tenant", True, None), ("ai_kill_switch", None, None, None)),
        (KillSwitchChanged(T, "tenant", False, None), None),
        (EcosystemKillSwitchChanged(T, "tenant", True, None, None), ("kill_switch", None, None, None)),
        (EcosystemKillSwitchChanged(T, "platform_app", True, A, None), ("kill_switch", None, None, A)),
        (McpEnablementChanged(T, False, ("core",), None, None), ("mcp_disabled", None, None, None)),
        (McpEnablementChanged(T, True, ("core",), "v1", None), None),
        (InstallationChanged(T, I, "app", A, None, "uninstalled", "uninstalled", "user"), ("uninstalled", I, None, None)),
        (InstallationChanged(T, I, "app", A, None, "suspended", "suspended", "user"), ("uninstalled", I, None, None)),
        (InstallationChanged(T, I, "app", A, None, "granted", "active", "user"), None),
        (UserGrantChanged(T, I, U, "mcp", "revoked"), ("authorization_revoked", I, U, None)),
        (UserGrantChanged(T, I, U, "api", "revoked"), None),
        (OAuthTokenFamilyRevoked(T, I, uuid.uuid4(), U, "user"), ("authorization_revoked", I, U, None)),
        (OAuthTokenFamilyRevoked(T, I, uuid.uuid4(), None, "user"), None),
    ],
)
def test_event_mapping(event: object, expected: tuple | None) -> None:
    req = lifecycle.for_event(event)
    if expected is None:
        assert req is None
    else:
        assert req is not None and (req.reason, req.installation_id, req.user_id, req.app_id) == expected


async def _status(env: McpEnv) -> tuple[str, str | None]:
    async with tenant_session(env.org.tenant_id) as db:
        row = (await db.execute(select(McpSession).order_by(McpSession.started_at.desc()))).scalars().first()
        assert row is not None
        return row.status, row.end_reason


async def test_ai_kill_switch_ends_sessions(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    async with tenant_session(mcp_env.org.tenant_id) as db:
        await agent_runtime_service.set_kill_switch(mcp_env.human_ctx(), db, on=True, reason="drill")
    assert await _status(mcp_env) == ("ended", "ai_kill_switch")
    assert (await c.rpc("ping")).status_code in (403, 404)
    async with tenant_session(mcp_env.org.tenant_id) as db:
        await agent_runtime_service.set_kill_switch(mcp_env.human_ctx(), db, on=False, reason=None)


async def test_disable_ends_sessions_and_gates_refuse(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    async with tenant_session(mcp_env.org.tenant_id) as db:
        await mcp_service.disable(mcp_env.human_ctx(), db, reason="pause")
    assert await _status(mcp_env) == ("ended", "mcp_disabled")
    resp = await c.rpc("ping")
    assert resp.status_code == 401


async def test_revocation_and_uninstall_end_matching_sessions(mcp_env: McpEnv) -> None:
    await mcp_env.client()
    org = mcp_env.org
    ended = await mcp_service.end_sessions(org.tenant_id, reason="authorization_revoked", installation_id=uuid.uuid4(), user_id=org.admin.user_id)
    assert ended == 0
    ended = await mcp_service.end_sessions(org.tenant_id, reason="authorization_revoked", installation_id=mcp_env.installation.id, user_id=org.admin.user_id)
    assert ended == 1 and await _status(mcp_env) == ("ended", "authorization_revoked")
    await mcp_env.client()
    assert await mcp_service.end_sessions(org.tenant_id, reason="kill_switch", app_id=mcp_env.app_row.id) == 1


async def test_gates_refuse_within_the_cache_window_without_the_task(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    async with tenant_session(mcp_env.org.tenant_id) as db:
        from app.services import ai

        await ai.set_features(db, mcp_env.org.tenant_id, {"agent_mcp": False})
    mcp_auth.gate_cache.clear()  # what the 15 s TTL does on its own
    assert (await c.rpc("ping")).status_code == 403


async def test_idle_run_is_mirrored(mcp_env: McpEnv) -> None:
    c = await mcp_env.client()
    async with tenant_session(mcp_env.org.tenant_id) as db:
        row = (await db.execute(select(McpSession))).scalar_one()
        await agent_runtime_service.close_external_session(RequestContext(tenant_id=mcp_env.org.tenant_id), db, row.run_id, reason="timeout")
        run = await db.get(AgentRun, row.run_id)
        assert run is not None and run.status == "stopped"
    resp = await c.rpc("ping")
    assert resp.status_code == 404 and "idle" in resp.json()["error"]["message"]
    assert await _status(mcp_env) == ("ended", "idle")
