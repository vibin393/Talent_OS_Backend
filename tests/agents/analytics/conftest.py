# ruff: noqa: E501, F401
"""Epic 64 test harness: the report-builder org (``rorg``: admin, recruiter, manager, account manager, recruiter B with
seeded placements via ``seed_placements``), the analytics agent enabled, and a Fake-LLM runner."""

from collections.abc import Iterator

import pytest

from app.core import rate_limit
from app.core.settings import get_settings
from app.services.reporting import common
from app.services.reporting.builder import hooks as builder_hooks
from app.services.reporting.builder import runs
from tests.reporting.conftest import other_rorg, rorg


@pytest.fixture(autouse=True)
def _analytics_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "dashboard_cache_seconds", 0)
    monkeypatch.setattr(get_settings(), "clamav_host", "")
    rate_limit.set_backend(rate_limit.memory_backend())
    rate_limit.memory_backend().reset()
    common.reset_availability()
    builder_hooks.register()
    runs.clear_cache()
    yield
    rate_limit.set_backend(None)
    rate_limit.memory_backend().reset()
