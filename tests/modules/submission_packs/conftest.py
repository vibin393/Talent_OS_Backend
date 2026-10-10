"""Epic 61 fixtures: ``seed`` -- a shortlisted Application on a fresh tenant (``helpers.seed_pack``)."""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI

from tests.epic3 import _isolated_files, _no_real_ai  # noqa: F401
from tests.modules.identity.conftest import join, make_tenant
from tests.modules.pipeline.helpers import _fresh_limits, outbox  # noqa: F401
from tests.modules.submission_packs.helpers import PackSeed, seed_pack


@pytest.fixture
async def seed(app: FastAPI) -> AsyncIterator[PackSeed]:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name="Ada Admin")
    s = await seed_pack(admin, tenant.id)
    yield s
    await admin.client.aclose()


@pytest.fixture
async def other_seed(app: FastAPI) -> AsyncIterator[PackSeed]:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name="Olga Other")
    s = await seed_pack(admin, tenant.id, name="Other Person")
    yield s
    await admin.client.aclose()
