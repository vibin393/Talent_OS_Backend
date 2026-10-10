"""Epic 63 fixtures: ``cseed`` -- a Client with billing contacts and a collection rule on a fresh tenant."""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI

from tests.epic3 import _isolated_files, _no_real_ai  # noqa: F401
from tests.modules.collection_rules.helpers import CollSeed, seed_collections
from tests.modules.identity.conftest import join, make_tenant
from tests.modules.pipeline.helpers import _fresh_limits, outbox  # noqa: F401


@pytest.fixture
async def cseed(app: FastAPI) -> AsyncIterator[CollSeed]:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name="Fiona Finance")
    s = await seed_collections(admin, tenant)
    yield s
    await admin.client.aclose()


@pytest.fixture
async def other_cseed(app: FastAPI) -> AsyncIterator[CollSeed]:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name="Olga Other")
    s = await seed_collections(admin, tenant)
    yield s
    await admin.client.aclose()
