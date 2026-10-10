# ruff: noqa: E501
"""Epic 53 agent runtime test harness.

* ``_test_agent`` -- a test-only agent (key with a leading ``_`` -> gated by the ``agentic`` master switch only) whose
  allow-list is the core tools; ``_test_conversational`` adds waits. Registered once per process.
* ``agent_org`` -- a tenant with the agentic master switch on, its Tenant Admin (owner of runs) and a candidate.
* Model calls use the scripted Fake LLM (``fake_llm``); tests never touch the network.
"""

import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.agentic import AgentSpec, RunLimits, agents, register_agent, tools
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.hooks import autodiscover
from app.core.permissions import Principal, Scope
from app.core.settings import get_settings
from app.services import ai
from app.services.agent_runtime import locks
from tests.epic3 import _isolated_files, _no_real_ai, create_candidate  # noqa: F401
from tests.modules.identity.conftest import Member, join, make_tenant

CORE_TOOLS = (
    "tasks.create", "notes.add", "flags.raise", "notify.user", "consent.status",
    "messaging.create_draft", "messaging.send_templated", "messaging.send", "knowledge.search",
)  # fmt: skip
CEILING = dict.fromkeys(
    (
        "task:create", "note:write", "candidate:read", "message:draft", "message:send_templated", "email:send",
        "messaging:send", "knowledge:read", "application:read", "demand:read",
    ),
    Scope.TENANT,
)  # fmt: skip


def _register_test_agents() -> None:
    autodiscover("app.services")
    assert set(CORE_TOOLS) <= set(tools())
    if "_test_agent" not in agents():
        register_agent(
            AgentSpec(
                key="_test_agent",
                version=1,
                owner_epic="53",
                name="Test Agent",
                purpose="Governance tests",
                prompt_id="agent__test_agent",
                tools=CORE_TOOLS,
                permission_ceiling=CEILING,
                default_mode="auto",
                limits=RunLimits(max_wait_hours=0),
            )
        )
    if "_test_conversational" not in agents():
        register_agent(
            AgentSpec(
                key="_test_conversational",
                version=1,
                owner_epic="53",
                name="Test Conversational Agent",
                purpose="Wait tests",
                prompt_id="agent__test_conversational",
                tools=CORE_TOOLS,
                permission_ceiling=CEILING,
                default_mode="auto",
                origin="conversation",
            )
        )


@dataclasses.dataclass
class AgentOrg:
    tenant_id: uuid.UUID
    admin: Member
    tenant: Any
    candidate_id: uuid.UUID

    def ctx(self) -> RequestContext:
        return RequestContext(
            tenant_id=self.tenant_id,
            principal=Principal(type="user", id=self.admin.user_id, tenant_id=self.tenant_id),
        )


async def enable(tenant_id: uuid.UUID, **features: bool) -> None:
    async with tenant_session(tenant_id) as db:
        await ai.set_features(db, tenant_id, {"agentic": True, **features})


@pytest.fixture(scope="session", autouse=True)
def _agents_registered() -> None:
    _register_test_agents()


@pytest.fixture(autouse=True)
def _agent_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "ai_provider", "anthropic")
    monkeypatch.setattr(get_settings(), "ai_fake_when_keyless", True)
    locks.force_local(False)
    yield
    ai.set_client_factory(None)


@pytest.fixture
async def agent_org(app: FastAPI) -> AsyncIterator[AgentOrg]:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name="Ada Admin")
    await enable(tenant.id)
    cand = await create_candidate(
        admin, full_name="Priya Raman", emails=[{"email": "priya.raman@example.com"}]
    )
    yield AgentOrg(tenant_id=tenant.id, admin=admin, tenant=tenant, candidate_id=uuid.UUID(cand["id"]))
    await admin.client.aclose()


@pytest.fixture
async def other_org(app: FastAPI) -> AsyncIterator[AgentOrg]:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name="Olga Other")
    await enable(tenant.id)
    cand = await create_candidate(admin, full_name="Other Person")
    yield AgentOrg(tenant_id=tenant.id, admin=admin, tenant=tenant, candidate_id=uuid.UUID(cand["id"]))
    await admin.client.aclose()
