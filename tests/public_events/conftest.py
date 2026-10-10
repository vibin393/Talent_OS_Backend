"""Epic 66 (AD-79) public events test fixtures: hooks discovered, sinks isolated, the after-commit kick controllable."""

import uuid
from collections.abc import Iterator

import pytest

from app.core import public_events as registry
from app.core.hooks import autodiscover
from app.services.public_events import sinks as sinks_mod
from tests.factories import create_tenant_row


@pytest.fixture(scope="session", autouse=True)
def _discovered() -> None:
    autodiscover("app.services")


@pytest.fixture(autouse=True)
def isolated_sinks() -> Iterator[None]:
    """Run each test with no sink but its own (other owners' sinks are restored afterwards)."""
    saved = dict(sinks_mod._sinks._items)
    sinks_mod._sinks._items.clear()
    yield
    sinks_mod._sinks._items.clear()
    sinks_mod._sinks._items.update(saved)


@pytest.fixture
def no_kick() -> Iterator[None]:
    """Simulate a crash between the outbox write and the relay: the after-commit kick never runs."""
    previous = registry.get_dispatch_kicker()
    registry.set_dispatch_kicker(None)
    yield
    registry.set_dispatch_kicker(previous)


@pytest.fixture
async def tenant() -> uuid.UUID:
    return await create_tenant_row("Public Events Tenant")
