"""Story 12.7 (FR-75): scoped API keys shown once, bearer authentication, request log, per-key rate limit,
rotation grace and revocation. Matrix rows 25-27, 29, 35."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core import rate_limit
from app.core.database import tenant_session
from app.core.settings import get_settings
from app.models.api_keys import ApiKey, ApiKeyLookup, ApiKeyRequestLog
from tests.epic12 import Org3, create_candidate, h, member
from tests.modules.identity.conftest import new_client

KEYS = "/api/v1/api-keys"


async def _create(org: Org3, **body: object) -> dict[str, object]:
    resp = await org.admin.client.post(
        KEYS, json={"name": "ATS sync", "scopes": ["candidate:read"], **body}, headers=h(org.admin)
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_key_shown_once_hash_only_get_ok_post_403_logged(app: FastAPI, org3: Org3) -> None:
    """Matrix 25."""
    await create_candidate(org3.admin)
    created = await _create(org3)
    key = str(created["key"])
    assert key.startswith(f"tos_{created['prefix']}_")
    listed = (await org3.admin.client.get(KEYS)).json()
    assert "key" not in listed[0] and listed[0]["status"] == "active"
    async with tenant_session(org3.tenant_id) as db:
        row = (await db.execute(select(ApiKey))).scalar_one()
        assert key.encode() not in row.key_hash and len(row.key_hash) == 32
        assert (await db.execute(select(ApiKeyLookup).where(ApiKeyLookup.key_id == row.id))).first() is not None

    async with new_client(app) as client:
        client.cookies.update(org3.admin.client.cookies)  # cookies are ignored with a bearer token
        auth = {"Authorization": f"Bearer {key}"}
        got = await client.get("/api/v1/candidates", headers=auth)
        assert got.status_code == 200, got.text
        assert got.json()["total"] == 1
        post = await client.post("/api/v1/candidates", json={"full_name": "Nope"}, headers=auth)
        assert post.status_code == 403  # no CSRF needed, just no candidate:create scope
    async with tenant_session(org3.tenant_id) as db:
        logs = list((await db.execute(select(ApiKeyRequestLog).order_by(ApiKeyRequestLog.at))).scalars())
        key_row = (await db.execute(select(ApiKey))).scalar_one()
    assert [(log.method, log.status) for log in logs] == [("GET", 200), ("POST", 403)]
    assert logs[0].route_template == "/api/v1/candidates"
    assert key_row.last_used_at is not None
    usage = (await org3.admin.client.get(f"{KEYS}/{created['id']}/usage")).json()
    assert usage["by_status"] == {"200": 1, "403": 1}


async def test_scope_rules(app: FastAPI, org3: Org3) -> None:
    """Matrix 27: non-delegable -> 422; scopes the creator lacks -> 422 scope_not_held."""
    resp = await org3.admin.client.post(
        KEYS, json={"name": "x", "scopes": ["api_key:manage"]}, headers=h(org3.admin)
    )
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "scope_not_delegable")
    unknown = await org3.admin.client.post(KEYS, json={"name": "x", "scopes": ["candidate:fly"]}, headers=h(org3.admin))
    assert unknown.status_code == 422
    scopes = (await org3.admin.client.get(f"{KEYS}/delegable-scopes")).json()
    assert "candidate:read" in scopes and "api_key:manage" not in scopes and "dsr:process" not in scopes
    # A (custom) role holding api_key:manage but not candidate:create cannot delegate it.
    from app.models.identity import Role

    async with tenant_session(org3.tenant_id) as db:
        db.add(
            Role(
                id=uuid.uuid4(),
                tenant_id=org3.tenant_id,
                key="integrator",
                name="Integrator",
                permissions={"api_key:manage": "tenant", "candidate:read": "tenant"},
            )
        )
    integrator = await member(app, org3, "integrator", name="Ian Integrator")
    try:
        resp = await integrator.client.post(
            KEYS, json={"name": "x", "scopes": ["candidate:create"]}, headers=h(integrator)
        )
        assert (resp.status_code, resp.json()["error"]["code"]) == (422, "scope_not_held")
    finally:
        await integrator.client.aclose()


async def test_rotation_grace_and_revocation(app: FastAPI, org3: Org3) -> None:
    """Matrix 26: revoked key or old key past its rotation grace -> 401 invalid_api_key."""
    old = await _create(org3)
    rotated = await org3.admin.client.post(f"{KEYS}/{old['id']}/rotate", json={"grace_hours": 1}, headers=h(org3.admin))
    assert rotated.status_code == 201, rotated.text
    new_key = rotated.json()["key"]
    assert rotated.json()["rotated_from_id"] == old["id"]
    async with new_client(app) as client:
        assert (await client.get("/api/v1/candidates", headers={"Authorization": f"Bearer {old['key']}"})).status_code == 200
        async with tenant_session(org3.tenant_id) as db:
            await db.execute(
                update(ApiKey)
                .where(ApiKey.id == uuid.UUID(str(old["id"])))
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        expired = await client.get("/api/v1/candidates", headers={"Authorization": f"Bearer {old['key']}"})
        assert (expired.status_code, expired.json()["error"]["code"]) == (401, "invalid_api_key")
        assert (await client.get("/api/v1/candidates", headers={"Authorization": f"Bearer {new_key}"})).status_code == 200
        await org3.admin.client.post(f"{KEYS}/{rotated.json()['id']}/revoke", headers=h(org3.admin))
        revoked = await client.get("/api/v1/candidates", headers={"Authorization": f"Bearer {new_key}"})
        assert (revoked.status_code, revoked.json()["error"]["code"]) == (401, "invalid_api_key")
        garbage = await client.get("/api/v1/candidates", headers={"Authorization": "Bearer tos_deadbeef_" + "x" * 43})
        assert garbage.status_code == 401
    audit_rows = (await org3.admin.client.get("/api/v1/admin/audit-logs", params={"action": "api_key."})).json()
    assert "api_key.auth_failed" in {e["action"] for e in audit_rows["items"]}


async def test_per_key_rate_limit(app: FastAPI, org3: Org3, monkeypatch: pytest.MonkeyPatch) -> None:
    """Matrix 29: limit 60/min, the 61st request -> 429."""
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    rate_limit.memory_backend().reset()
    created = await _create(org3, rate_limit_per_min=60)
    async with new_client(app) as client:
        auth = {"Authorization": f"Bearer {created['key']}"}
        statuses = [(await client.get("/api/v1/ai/features", headers=auth)).status_code for _ in range(61)]
    assert statuses[:60] == [200] * 60
    assert statuses[60] == 429
    rate_limit.memory_backend().reset()


async def test_cross_tenant_and_permissions(app: FastAPI, org3: Org3, other3: Org3) -> None:
    theirs = await _create(other3)
    for method, url, body in [
        ("post", f"{KEYS}/{theirs['id']}/rotate", {"grace_hours": 0}),
        ("post", f"{KEYS}/{theirs['id']}/revoke", {}),
        ("get", f"{KEYS}/{theirs['id']}/usage", None),
    ]:
        kwargs = {} if body is None else {"json": body, "headers": h(org3.admin)}
        assert (await getattr(org3.admin.client, method)(url, **kwargs)).status_code == 404
    # Their key only ever sees their tenant.
    await create_candidate(org3.admin)
    async with new_client(app) as client:
        resp = await client.get("/api/v1/candidates", headers={"Authorization": f"Bearer {theirs['key']}"})
        assert resp.json()["total"] == 0
    recruiter = await member(app, org3, "recruiter")
    try:
        assert (await recruiter.client.get(KEYS)).status_code == 403
    finally:
        await recruiter.client.aclose()
