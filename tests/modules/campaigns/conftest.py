# ruff: noqa: E501
"""Epic 39 fixtures. Emails go to the outbox mailer (forced by the root conftest); SMS / WhatsApp use the fake
adapters; never a real provider. The open pixel needs an https link base. Send windows are bypassed unless a test is
marked ``real_windows`` (tests run at any wall-clock time)."""

from collections.abc import Iterator
from datetime import datetime

import pytest

from app.core.settings import get_settings
from app.services.campaigns import governance, throttle
from app.services.connectors.sms.fake import FakeSmsProvider
from app.services.connectors.whatsapp.fake import FakeWhatsAppProvider
from tests.epic3 import _isolated_files, _no_real_ai  # noqa: F401
from tests.epic10 import dorg, mailbox, other_dorg, outbox  # noqa: F401
from tests.modules.interviews.helpers import _connections_env  # noqa: F401


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "real_windows: keep the recipient send-window rule active")


@pytest.fixture(autouse=True)
def _marketing_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "marketing_link_base_url", "https://app.example.test")
    throttle.memory_backend().reset()
    FakeSmsProvider.reset()
    FakeWhatsAppProvider.reset()
    if request.node.get_closest_marker("real_windows") is None:

        async def _always_open(*_: object, **__: object) -> datetime | None:
            return None

        monkeypatch.setattr(governance, "window_defer", _always_open)
    yield
    throttle.memory_backend().reset()
    FakeSmsProvider.reset()
    FakeWhatsAppProvider.reset()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Load tests (``@pytest.mark.slow``) only run when selected with ``-m slow`` (mirrors ``tests/perf/conftest.py``)."""
    if "slow" in (config.getoption("markexpr") or ""):
        return
    skip = pytest.mark.skip(reason="load test: run with -m slow")
    here = __file__.rsplit("conftest.py", 1)[0]
    for item in items:
        if "slow" in item.keywords and str(item.fspath).startswith(here):
            item.add_marker(skip)
