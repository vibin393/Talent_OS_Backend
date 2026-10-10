# ruff: noqa: E501
"""Metering test helpers: rate limiting on with the in-memory backend, a clean metering buffer, a frozen clock."""

import time
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text

from app.core import rate_limit
from app.core.database import platform_session
from app.core.settings import get_settings
from app.services.metering import enforce
from app.services.metering import record as rec
from tests.factories import create_tenant_row


@pytest.fixture
def limits_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    monkeypatch.setattr(get_settings(), "rate_limit_backend", "memory")
    rate_limit.memory_backend().reset()
    enforce.clear_tier_cache()
    yield
    rate_limit.set_backend(None)
    rate_limit.memory_backend().reset()
    enforce.clear_tier_cache()


@pytest.fixture
def clean_buffer() -> Iterator[None]:
    rec.buffer_drain()
    yield
    rec.buffer_drain()


class Clock:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, start: float) -> None:
        self.now = start
        monkeypatch.setattr(time, "time", lambda: self.now)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    # 2026-10-09 12:00:00.5 UTC
    return Clock(monkeypatch, 1791547200.5)


@pytest.fixture
async def tenant() -> uuid.UUID:
    return await create_tenant_row("Metering Tenant")


async def set_tenant(tenant_id: uuid.UUID, **values: object) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in values)
    async with platform_session() as db:
        await db.execute(text(f"UPDATE tenants SET {sets} WHERE id = :id"), {**values, "id": tenant_id})
