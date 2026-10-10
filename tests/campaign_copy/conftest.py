# ruff: noqa: E501
"""Epic 78 (campaign copy) fixtures: the P3 marketing env (fake SMS / WhatsApp, open send windows), the Fake LLM only
(keyless fallback; tests never touch the network) and a tenant (``dorg``) with marketing settings."""

from collections.abc import Iterator

import pytest

from app.core.settings import get_settings
from app.services import ai
from app.services.agent_runtime import locks
from tests.epic3 import _isolated_files, _no_real_ai  # noqa: F401
from tests.epic10 import dorg, mailbox, other_dorg, outbox  # noqa: F401
from tests.modules.campaigns.conftest import _marketing_env  # noqa: F401
from tests.modules.interviews.helpers import _connections_env  # noqa: F401


@pytest.fixture(autouse=True)
def _agent_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "ai_provider", "anthropic")
    monkeypatch.setattr(get_settings(), "ai_fake_when_keyless", True)
    locks.force_local(False)
    yield
    ai.set_client_factory(None)
