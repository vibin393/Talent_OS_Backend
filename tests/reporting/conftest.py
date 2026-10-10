from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI

from app.core import rate_limit
from app.core.settings import get_settings
from app.services.reporting import common
from tests.epic3 import _isolated_files  # noqa: F401
from tests.reporting.helpers import ROrg, make_rorg


@pytest.fixture(autouse=True)
def _reporting_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No tile cache between assertions, fresh rate-limit counters, optional tables re-checked."""
    monkeypatch.setattr(get_settings(), "dashboard_cache_seconds", 0)
    monkeypatch.setattr(get_settings(), "clamav_host", "")
    rate_limit.set_backend(rate_limit.memory_backend())
    rate_limit.memory_backend().reset()
    common.reset_availability()
    yield
    rate_limit.set_backend(None)
    rate_limit.memory_backend().reset()


@pytest.fixture
async def rorg(app: FastAPI) -> AsyncIterator[ROrg]:
    org = await make_rorg(app)
    yield org
    await org.close()


@pytest.fixture
async def other_rorg(app: FastAPI) -> AsyncIterator[ROrg]:
    org = await make_rorg(app, "Olga Other")
    yield org
    await org.close()
