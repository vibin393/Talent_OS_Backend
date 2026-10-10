# ruff: noqa: E501
"""Epic 63 fixtures: collections seeds on the Epic 53 ``agent_org``; the billing ``borg`` and the timesheet ``dorg``
orgs (with the agentic master switch turned on by the tests)."""

from collections.abc import AsyncIterator

import pytest

from tests.agents.conftest import AgentOrg
from tests.modules.billing.conftest import borg, other_borg  # noqa: F401
from tests.modules.collection_rules.helpers import CollSeed, enable_agent, seed_collections
from tests.modules.pipeline.helpers import _fresh_limits, outbox  # noqa: F401
from tests.modules.placements.conftest import dorg, other_dorg  # noqa: F401


@pytest.fixture
async def coll(agent_org: AgentOrg) -> AsyncIterator[CollSeed]:
    s = await seed_collections(agent_org.admin, agent_org.tenant)
    await enable_agent(agent_org.tenant_id)
    yield s


@pytest.fixture
async def coll_auto(agent_org: AgentOrg) -> AsyncIterator[CollSeed]:
    s = await seed_collections(agent_org.admin, agent_org.tenant, allow_auto=True)
    await enable_agent(agent_org.tenant_id)
    yield s
