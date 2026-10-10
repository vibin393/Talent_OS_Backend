# ruff: noqa: E501
"""Epic 61 fixtures: a shortlisted Application on the Epic 53 ``agent_org`` with the Submission Agent enabled."""

from collections.abc import AsyncIterator

import pytest

from tests.agents.conftest import AgentOrg
from tests.modules.pipeline.helpers import _fresh_limits, outbox  # noqa: F401
from tests.modules.submission_packs.helpers import PackSeed, enable_agent, seed_pack


@pytest.fixture
async def seed(agent_org: AgentOrg) -> AsyncIterator[PackSeed]:
    s = await seed_pack(agent_org.admin, agent_org.tenant_id)
    await enable_agent(agent_org.tenant_id)
    yield s
