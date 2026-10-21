# ruff: noqa: E501
"""``GET /api/v1/internal/tenant-ats-key`` (unified platform spoke -> hub): the per-tenant ATS key endpoint.

Covers auth (401 without / with the wrong secret), 404 for an unknown tenant, 200 returning a usable key,
idempotency (two calls -> the same key), and that the returned key actually authenticates against a Public API
v1 endpoint for that tenant (it writes a candidate into that tenant's silo)."""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.settings import get_settings
from app.services import internal_integration_service as svc
from tests.epic3 import Org3
from tests.modules.identity.conftest import new_client
from tests.public_api.e67 import CONSENT

SECRET = "test-internal-service-secret-value"
PATH = "/api/v1/internal/tenant-ats-key"


@pytest.fixture(autouse=True)
def _internal_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # The settings object is a cached singleton; patch the attribute for the duration of the test.
    monkeypatch.setattr(get_settings(), "internal_service_secret", SECRET)


async def _call(app: FastAPI, tenant_id: uuid.UUID | str, *, secret: str | None = SECRET) -> tuple[int, dict[str, Any]]:
    headers = {"X-Internal-Secret": secret} if secret is not None else {}
    async with new_client(app) as c:
        resp = await c.get(PATH, params={"tenant_id": str(tenant_id)}, headers=headers)
    body = resp.json() if resp.content else {}
    return resp.status_code, body


async def test_requires_internal_secret(app: FastAPI, org3: Org3) -> None:
    status, _ = await _call(app, org3.tenant_id, secret=None)
    assert status == 401
    status, _ = await _call(app, org3.tenant_id, secret="wrong-secret")
    assert status == 401


async def test_empty_configured_secret_refuses(app: FastAPI, org3: Org3, monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail closed: an unset INTERNAL_SERVICE_SECRET refuses every call, even with a matching empty header.
    monkeypatch.setattr(get_settings(), "internal_service_secret", "")
    assert (await _call(app, org3.tenant_id, secret=""))[0] == 401
    assert (await _call(app, org3.tenant_id, secret=SECRET))[0] == 401


async def test_unknown_tenant_404(app: FastAPI) -> None:
    status, _ = await _call(app, uuid.uuid4())
    assert status == 404


async def test_returns_usable_idempotent_key(app: FastAPI, org3: Org3) -> None:
    status, body = await _call(app, org3.tenant_id)
    assert status == 200, body
    assert set(body) == {"api_key", "base_url", "owner_id", "client_id"}
    key = body["api_key"]
    assert key.startswith("tos_")
    assert body["base_url"] == svc.ATS_PUBLIC_API_BASE_URL
    # owner_id is a real in-tenant user (the tenant admin); client_id is the default integration Client.
    assert body["owner_id"] == str(org3.admin.user_id)
    assert body["client_id"] is not None

    # Idempotent: a second call returns the SAME key (decrypted ciphertext) and a STABLE client_id (find-or-create).
    status2, body2 = await _call(app, org3.tenant_id)
    assert status2 == 200
    assert body2["api_key"] == key
    assert body2["owner_id"] == body["owner_id"]
    assert body2["client_id"] == body["client_id"]

    # The key + returned owner_id write a candidate into THIS tenant's silo.
    async with new_client(app) as c:
        created = await c.post(
            "/api/public/v1/candidates",
            json={"full_name": "Spoke Pushed", "owner_id": body["owner_id"], "consent": CONSENT},
            headers={"Authorization": f"Bearer {key}", "Idempotency-Key": uuid.uuid4().hex},
        )
    assert created.status_code == 201, created.text
    assert created.json()["full_name"] == "Spoke Pushed"

    # The returned client_id + owner_id let a demand be filed in THIS tenant (the coordinator's failing case).
    async with new_client(app) as c:
        demand = await c.post(
            "/api/public/v1/demands",
            json={"title": "Spoke Role", "client_id": body["client_id"], "owner_id": body["owner_id"]},
            headers={"Authorization": f"Bearer {key}", "Idempotency-Key": uuid.uuid4().hex},
        )
    assert demand.status_code == 201, demand.text


async def test_client_is_stable_find_or_create(app: FastAPI, org3: Org3) -> None:
    from sqlalchemy import text

    from app.core.database import tenant_session

    first = (await _call(app, org3.tenant_id))[1]["client_id"]
    second = (await _call(app, org3.tenant_id))[1]["client_id"]
    assert first is not None and first == second  # reused, not recreated
    async with tenant_session(org3.tenant_id) as db:
        n = (await db.execute(text("SELECT count(*) FROM clients WHERE name = :n"), {"n": svc.PARTNER_CLIENT_NAME})).scalar_one()
    assert n == 1  # exactly one integration Client, however many times the endpoint is called


async def test_remint_when_stored_key_revoked(app: FastAPI, org3: Org3) -> None:
    from app.core.context import RequestContext
    from app.core.database import tenant_session
    from app.services import api_keys_service

    status, body = await _call(app, org3.tenant_id)
    assert status == 200
    first_key = body["api_key"]

    # Revoke the stored key directly in the tenant silo, then call again: a fresh key is minted and returned.
    async with tenant_session(org3.tenant_id) as db:
        cred_key_id = (await _stored_key_id(org3.tenant_id))
        await api_keys_service.revoke_key(RequestContext(tenant_id=org3.tenant_id), db, cred_key_id)

    status2, body2 = await _call(app, org3.tenant_id)
    assert status2 == 200
    assert body2["api_key"] != first_key
    # The fresh key works on the Public API.
    async with new_client(app) as c:
        resp = await c.get("/api/public/v1/candidates", headers={"Authorization": f"Bearer {body2['api_key']}"})
    assert resp.status_code == 200, resp.text


async def _stored_key_id(tenant_id: uuid.UUID) -> uuid.UUID:
    from app.core.database import platform_session
    from app.models.integration_credentials import TenantIntegrationCredential

    async with platform_session() as pdb:
        cred = await pdb.get(TenantIntegrationCredential, (tenant_id, svc.ATS_SERVICE))
    assert cred is not None
    return cred.api_key_id
