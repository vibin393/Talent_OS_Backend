"""Epic 56 fixtures: a seeded sourcing Demand on the Epic 53 ``agent_org`` (``helpers.seed_sourcing``)."""

from collections.abc import AsyncIterator

import pytest

from tests.agents.conftest import AgentOrg
from tests.agents.sourcing.helpers import Seed, seed_sourcing


@pytest.fixture
async def seed(agent_org: AgentOrg) -> AsyncIterator[Seed]:
    yield await seed_sourcing(agent_org.admin, agent_org.tenant_id)
