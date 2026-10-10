from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _leave_sources() -> Iterator[None]:
    """Other suites register fake prefill sources and unregister them afterwards; re-register the real
    ones."""
    from app.services.time import leave_hooks

    leave_hooks._prefill()
    yield
    leave_hooks._prefill()
