from collections.abc import Iterator

import pytest

from app.services.reporting.builder import hooks as builder_hooks
from app.services.reporting.builder import runs


@pytest.fixture(autouse=True)
def _builder_env() -> Iterator[None]:
    """Sources / notification types registered; no pin-run cache between assertions."""
    builder_hooks.register()
    runs.clear_cache()
    yield
    runs.clear_cache()
