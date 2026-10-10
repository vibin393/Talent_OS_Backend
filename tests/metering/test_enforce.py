# ruff: noqa: E501
"""Rate Tier enforcement: burst, per minute, delegated user, daily quota (``quota_exceeded`` until 00:00 UTC)."""

import uuid
from typing import Any

import pytest

from app.core.errors import RateLimited
from app.core.permissions import Principal
from app.core.settings import get_settings
from app.services import metering_service
from tests.metering.conftest import Clock


class _Resp:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


def _headers(exc: RateLimited) -> dict[str, str]:
    resp = _Resp()
    for hook in exc.response_hooks:
        hook(resp)
    return resp.headers


def _limits(monkeypatch: pytest.MonkeyPatch, **values: int) -> None:
    base = {"per_minute": 1000, "burst_per_second": 1000, "per_day": 100_000, "delegated_user_per_min": 1000}
    monkeypatch.setattr(get_settings(), "rate_tiers", {"standard": {**base, **values}})


def _app(tenant: uuid.UUID, inst: uuid.UUID | None = None, user: uuid.UUID | None = None) -> Principal:
    inst = inst or uuid.uuid4()
    return Principal(type="app", id=inst, tenant_id=tenant, app_installation_id=inst, acting_user_id=user)


async def _calls(p: Any, n: int) -> None:
    for _ in range(n):
        await metering_service.enforce(p, "candidates")


async def test_disabled_is_noop(tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
    _limits(monkeypatch, per_minute=1, burst_per_second=1, per_day=1)
    await _calls(_app(tenant), 5)


async def test_burst_per_second(
    tenant: uuid.UUID, limits_on: None, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _limits(monkeypatch, burst_per_second=3)
    p = _app(tenant)
    await _calls(p, 3)
    with pytest.raises(RateLimited) as err:
        await metering_service.enforce(p, "candidates")
    assert err.value.code == "rate_limited"
    assert int(_headers(err.value)["Retry-After"]) >= 1
    await _calls(_app(tenant), 3)  # another installation has its own window


async def test_per_minute(
    tenant: uuid.UUID, limits_on: None, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _limits(monkeypatch, per_minute=5)
    p = _app(tenant)
    await _calls(p, 5)
    with pytest.raises(RateLimited) as err:
        await metering_service.enforce(p, "candidates")
    assert err.value.code == "rate_limited"
    assert "Retry-After" in _headers(err.value)


async def test_delegated_user_sub_limit(
    tenant: uuid.UUID, limits_on: None, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _limits(monkeypatch, delegated_user_per_min=2)
    inst, user = uuid.uuid4(), uuid.uuid4()
    await _calls(_app(tenant, inst, user), 2)
    with pytest.raises(RateLimited):
        await metering_service.enforce(_app(tenant, inst, user), "candidates")
    # the same user through another installation shares the user sub-limit
    with pytest.raises(RateLimited):
        await metering_service.enforce(_app(tenant, None, user), "candidates")
    # another user (and the installation without a user) still pass
    await _calls(_app(tenant, inst, uuid.uuid4()), 2)
    await _calls(_app(tenant, inst), 2)


async def test_daily_quota_resets_at_utc_midnight(
    tenant: uuid.UUID, limits_on: None, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _limits(monkeypatch, per_day=4)
    clock.now = 1791590340.0  # 2026-10-09 23:59:00 UTC
    p = _app(tenant)
    await _calls(p, 2)
    # the day quota is per tenant: an API key of the same tenant shares it
    key = Principal(type="api_key", id=uuid.uuid4(), tenant_id=tenant, api_key_id=uuid.uuid4())
    await _calls(key, 2)
    with pytest.raises(RateLimited) as err:
        await metering_service.enforce(key, "candidates")
    assert err.value.code == "quota_exceeded"
    assert _headers(err.value)["Retry-After"] == "60"
    with pytest.raises(RateLimited) as err2:
        await metering_service.enforce(p, "candidates")
    assert err2.value.code == "quota_exceeded"
    clock.now = 1791590400.5  # 2026-10-10 00:00:00.5 UTC
    await _calls(p, 4)
    with pytest.raises(RateLimited):
        await metering_service.enforce(p, "candidates")


async def test_api_keys_only_get_the_day_quota(
    tenant: uuid.UUID, limits_on: None, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API keys keep their own ``rate_limit_per_min`` (api_keys_service); the tier minute/burst do not apply."""
    _limits(monkeypatch, per_minute=1, burst_per_second=1, per_day=10)
    key = Principal(type="api_key", id=uuid.uuid4(), tenant_id=tenant, api_key_id=uuid.uuid4())
    await _calls(key, 10)
    with pytest.raises(RateLimited) as err:
        await metering_service.enforce(key, "candidates")
    assert err.value.code == "quota_exceeded"
