from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI

from tests.epic3 import _isolated_files, _no_real_ai, fake_ai  # noqa: F401
from tests.modules.demands.helpers import DOrg, _org
from tests.modules.legal_entities.conftest import provision


@pytest.fixture
async def dorg(app: FastAPI) -> AsyncIterator[DOrg]:
    """Yield a tenant with an admin, a client and its default Legal Entity (placements need one)."""
    org = await _org(app, "Ada Admin")
    await provision(org.tenant)
    yield org
    await org.admin.client.aclose()


@pytest.fixture
async def other_dorg(app: FastAPI) -> AsyncIterator[DOrg]:
    org = await _org(app, "Olga Other")
    await provision(org.tenant)
    yield org
    await org.admin.client.aclose()
