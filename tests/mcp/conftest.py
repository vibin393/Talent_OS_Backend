# ruff: noqa: E501, F401, F811
"""Epic 73 MCP test harness: a tenant with AI agents on (the agents harness), MCP enabled through the real enable flow,
an installed MCP App and delegated ``mcp`` tokens for the Tenant Admin, plus the scripted client
(``tests.support.mcp_client``). Everything runs over the ASGI app; no network."""

import dataclasses
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import Principal
from app.schemas.mcp import McpEnableIn
from app.services import mcp_service, oauth_service
from app.services.mcp_server import auth as mcp_auth
from app.services.mcp_server import limits as mcp_limits
from tests.agents.conftest import AgentOrg, _agent_env, _agents_registered, agent_org, enable, other_org
from tests.support.mcp_client import McpClient
from tests.support.oauth import install_app, make_app, mint

ALL_GROUPS = ["core", "recruiting", "interviews", "delivery", "analytics", "knowledge", "outreach"]
BASE_SCOPES = (
    "candidates:read", "demands:read", "applications:read", "interviews:read", "placements:read", "reports:read",
    "notes:write", "tasks:write", "tasks:read", "demands:write", "applications:write", "knowledge:read",
    "predictions:read", "messages:propose", "interviews:propose",
)  # fmt: skip
MCP_SCOPES = ("mcp:read", "mcp:draft", "mcp:propose")
ALL_SCOPES = (*MCP_SCOPES, *BASE_SCOPES, "offline_access")


@dataclasses.dataclass
class McpEnv:
    app: FastAPI
    org: AgentOrg
    installation: Any
    app_row: Any
    clients: list[McpClient] = dataclasses.field(default_factory=list)

    def human_ctx(self, user_id: uuid.UUID | None = None) -> RequestContext:
        return RequestContext(
            tenant_id=self.org.tenant_id,
            principal=Principal(type="user", id=user_id or self.org.admin.user_id, tenant_id=self.org.tenant_id),
        )

    async def token(self, scopes: Sequence[str] = ALL_SCOPES, user: uuid.UUID | None = None, resource: str = "mcp") -> str:
        issued = await mint(self.installation, user or self.org.admin.user_id, scopes=scopes, resource=resource)
        return issued.access_token

    async def client(self, scopes: Sequence[str] = ALL_SCOPES, *, user: uuid.UUID | None = None, init: bool = True, **kw: Any) -> McpClient:
        c = McpClient(self.app, await self.token(scopes, user), **kw)
        self.clients.append(c)
        if init:
            await c.initialize()
        return c


async def enable_mcp(org: AgentOrg, groups: Sequence[str] = ALL_GROUPS) -> None:
    ctx = RequestContext(tenant_id=org.tenant_id, principal=Principal(type="user", id=org.admin.user_id, tenant_id=org.tenant_id))
    async with tenant_session(org.tenant_id) as db:
        await mcp_service.enable(ctx, db, McpEnableIn(notice_version="mcp-notice-v1", acknowledged=True, tool_groups=list(groups)))


@pytest.fixture(autouse=True)
def _mcp_reset() -> None:
    mcp_auth.gate_cache.clear()
    mcp_limits.memory_store().reset()
    oauth_service.clear_cache()


async def make_env(app: FastAPI, org: AgentOrg, *, enabled: bool = True, scopes: Sequence[str] = ALL_SCOPES) -> McpEnv:
    if enabled:
        await enable_mcp(org)
    app_row = await make_app(name="Claude", scopes=scopes, grant_types=("authorization_code", "refresh_token"), resources=("mcp",))
    inst = await install_app(org.tenant_id, app_row, granted_by=org.admin.user_id)
    return McpEnv(app=app, org=org, installation=inst, app_row=app_row)


@pytest.fixture
async def mcp_env(app: FastAPI, agent_org: AgentOrg) -> AsyncIterator[McpEnv]:
    env = await make_env(app, agent_org)
    yield env
    for c in env.clients:
        await c.aclose()
