import re

import httpx
import pytest
from fastapi import status

import app.api
import app.router.base
from app.core.settings import Settings
from app.router.base import DBHealth

HEX32 = re.compile(r"^[0-9a-f]{32}$")


async def test_legacy_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"message": "All Healthy"}


async def test_legacy_pulse(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/pulse")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"message": "I'm Alive"}


async def test_v1_health_uses_runtime_role(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["db_role"] == "talent_app"


async def test_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/pulse", headers={"X-Request-ID": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"
    generated = await client.get("/api/pulse")
    assert len(generated.headers["x-request-id"]) == 32


@pytest.mark.parametrize("bad", ["x" * 129, "a\x01b", "a b"])
async def test_invalid_request_id_is_replaced(client: httpx.AsyncClient, bad: str) -> None:
    response = await client.get("/api/pulse", headers={"X-Request-ID": bad})
    assert HEX32.fullmatch(response.headers["x-request-id"])


async def _db_down() -> DBHealth:
    return DBHealth(False, status.HTTP_503_SERVICE_UNAVAILABLE, "Connection Refused")


async def test_v1_health_db_failure_returns_503_envelope(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.router.base, "check_database", _db_down)
    response = await client.get("/api/v1/health")
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "service_unavailable",
            "message": "Database unavailable",
            "details": {"database": "Connection Refused"},
        }
    }


async def test_v1_health_hides_internals_in_production(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    prod = Settings(
        app_env="production", db_app_password="not-the-default", audit_master_key="k", mfa_encryption_key="k"
    )
    monkeypatch.setattr(app.api, "get_settings", lambda: prod)
    ok = await client.get("/api/v1/health")
    assert ok.status_code == 200
    assert "db_role" not in ok.json()

    monkeypatch.setattr(app.router.base, "check_database", _db_down)
    down = await client.get("/api/v1/health")
    assert down.status_code == 503
    assert down.json()["error"]["details"] == {}
