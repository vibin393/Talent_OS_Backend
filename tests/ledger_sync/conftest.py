# ruff: noqa: F401
"""Epic 71 ledger sync fixtures: see ``tests.hrms_sync.helpers``."""

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI

from app.core.settings import get_settings
from tests.epic3 import _isolated_files, _no_real_ai, fake_ai
from tests.modules.receivables.conftest import ROrg, _org


@pytest.fixture(autouse=True)
def _e71_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from app.services import ecosystem_service
    from app.services.connectors.accounting.fake import FakeAccountingExporter
    from app.services.connectors.hrms.fake import FakeHrms
    from app.services.connectors.payroll.fake import FakePayrollProvider

    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
    ecosystem_service.bust_cache()
    FakeHrms.reset()
    FakeAccountingExporter.reset()
    FakePayrollProvider.reset()
    yield
    FakeHrms.reset()
    FakeAccountingExporter.reset()


@pytest.fixture
async def org(app: FastAPI) -> AsyncIterator[ROrg]:
    o = await _org(app, "Hema HR")
    yield o
    await o.admin.client.aclose()


@pytest.fixture
async def other(app: FastAPI) -> AsyncIterator[ROrg]:
    o = await _org(app, "Omar Other")
    yield o
    await o.admin.client.aclose()
