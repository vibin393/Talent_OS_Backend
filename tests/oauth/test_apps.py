# ruff: noqa: E501
"""Epic 67 (story 67.1): private App registration, show-once secrets, rotation overlap, revoke, retire, isolation."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.database import platform_session
from app.core.errors import ValidationFailed
from app.models.oauth import App, AppSecret
from app.services.oauth.apps import validate_redirect_uri
from tests.epic3 import Org3, h
from tests.oauth.e67 import REDIRECT, client_credentials, install, reasons, register


@pytest.mark.parametrize(
    ("uri", "client_type", "ok"),
    [
        ("https://a.example/cb", "confidential", True),
        ("http://a.example/cb", "confidential", False),
        ("http://127.0.0.1:8123/cb", "public", True),
        ("http://localhost:9000/cb", "public", True),
        ("http://127.0.0.1:8123/cb", "confidential", False),
        ("http://localhost/cb", "public", False),
        ("https://a.example/cb#frag", "confidential", False),
        ("https://*.example/cb", "confidential", False),
    ],
)
def test_redirect_rules(uri: str, client_type: str, ok: bool) -> None:
    if ok:
        assert validate_redirect_uri(uri, client_type) == uri
    else:
        with pytest.raises(ValidationFailed):
            validate_redirect_uri(uri, client_type)


async def test_registration_validation(org3: Org3) -> None:
    m = org3.admin
    base = {"name": "Acme", "redirect_uris": [REDIRECT], "requested_scopes": ["candidates:read"], "scope_reasons": reasons(["candidates:read"])}
    cases = [
        {**base, "scope_reasons": {"candidates:read": "short"}},  # reason 10-300
        {**base, "client_type": "public", "grant_types": ["client_credentials"]},  # public + cc
        {**base, "grant_types": ["client_credentials"], "resources": ["api", "mcp"]},  # mcp needs auth code
        {**base, "grant_types": ["authorization_code"], "redirect_uris": []},
        {**base, "requested_scopes": ["nope:read"], "scope_reasons": reasons(["nope:read"])},
    ]
    for body in cases:
        resp = await m.client.post("/api/v1/apps", json=body, headers=h(m))
        assert resp.status_code == 422, (body, resp.text)


async def test_secret_once_hash_stored_and_rotation_overlap(app: FastAPI, org3: Org3) -> None:
    m = org3.admin
    created = await register(m)
    secret = created["client_secret"]
    assert secret.startswith("tos_cs_") and created["client_id"].startswith("tosapp_")
    detail = (await m.client.get(f"/api/v1/apps/{created['id']}")).json()
    assert "client_secret" not in detail and secret not in str(detail)
    assert detail["secrets"][0]["fingerprint"] == secret[-4:]
    async with platform_session() as pdb:
        rows = list((await pdb.execute(select(AppSecret).where(AppSecret.app_id == uuid.UUID(created["id"])))).scalars())
    assert len(rows) == 1 and secret.encode() not in bytes(rows[0].secret_hash)
    await install(org3, created)
    # rotate: the old secret keeps working (overlap), a third secret is refused
    rot = await m.client.post(f"/api/v1/apps/{created['id']}/secrets/rotate", headers=h(m))
    assert rot.status_code == 201, rot.text
    assert rot.headers["cache-control"] == "no-store"
    new_secret = rot.json()["client_secret"]
    assert (await client_credentials(app, org3, created)).status_code == 200
    assert (await client_credentials(app, org3, {**created, "client_secret": new_secret})).status_code == 200
    third = await m.client.post(f"/api/v1/apps/{created['id']}/secrets/rotate", headers=h(m))
    assert (third.status_code, third.json()["error"]["code"]) == (409, "secret_limit")
    # the overlap ends (frozen clock: move the old secret's expiry into the past)
    async with platform_session() as pdb:
        await pdb.execute(
            update(AppSecret).where(AppSecret.id == rows[0].id).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    old = await client_credentials(app, org3, created)
    assert (old.status_code, old.json()["error"]) == (401, "invalid_client")
    # revoking the last valid secret needs confirmation
    new_id = rot.json()["secret_id"]
    resp = await m.client.post(f"/api/v1/apps/{created['id']}/secrets/{new_id}/revoke", json={}, headers=h(m))
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "last_secret")
    resp = await m.client.post(f"/api/v1/apps/{created['id']}/secrets/{new_id}/revoke", json={"confirm_last": True}, headers=h(m))
    assert resp.status_code == 204
    assert (await client_credentials(app, org3, {**created, "client_secret": new_secret})).status_code == 401


async def test_update_bumps_version_only_for_consent_changes(org3: Org3) -> None:
    m = org3.admin
    created = await register(m, scopes=("candidates:read",))
    await install(org3, created)
    resp = await m.client.patch(f"/api/v1/apps/{created['id']}", json={"name": "Acme Sync 2"}, headers=h(m))
    assert resp.status_code == 200 and resp.json()["manifest_version"] == 1
    body = {"requested_scopes": ["candidates:read", "demands:read"], "scope_reasons": reasons(["candidates:read", "demands:read"])}
    resp = await m.client.patch(f"/api/v1/apps/{created['id']}", json=body, headers=h(m))
    assert resp.status_code == 200, resp.text
    assert resp.json()["manifest_version"] == 2 and resp.json()["reconsent_installations"] == 1


async def test_retire_revokes_tokens(app: FastAPI, org3: Org3) -> None:
    m = org3.admin
    created = await register(m, scopes=("candidates:read",))
    await install(org3, created)
    tok = (await client_credentials(app, org3, created)).json()["access_token"]
    resp = await m.client.post(f"/api/v1/apps/{created['id']}/retire", json={"reason": "No longer used"}, headers=h(m))
    assert resp.status_code == 200 and resp.json()["status"] == "retired"
    from tests.oauth.e67 import public_get

    assert (await public_get(app, tok)).status_code == 401
    assert (await client_credentials(app, org3, created)).status_code == 401


async def test_cross_tenant_404_and_api_key_refused(org3: Org3, other3: Org3) -> None:
    created = await register(org3.admin)
    resp = await other3.admin.client.get(f"/api/v1/apps/{created['id']}")
    assert resp.status_code == 404
    resp = await other3.admin.client.patch(f"/api/v1/apps/{created['id']}", json={"name": "Hijack"}, headers=h(other3.admin))
    assert resp.status_code == 404
    assert (await other3.admin.client.get("/api/v1/apps")).json() == []
    # an API key cannot reach /api/v1/apps
    m = org3.admin
    key = await m.client.post("/api/v1/api-keys", json={"name": "k", "scopes": ["candidate:read"]}, headers=h(m))
    assert key.status_code == 201, key.text
    from tests.modules.identity.conftest import new_client

    async with new_client(m.client._transport.app) as c:  # type: ignore[attr-defined]
        resp = await c.get("/api/v1/apps", headers={"Authorization": f"Bearer {key.json()['key']}"})
    assert (resp.status_code, resp.json()["error"]["code"]) == (403, "human_session_required")


async def test_scope_catalogue_and_public_scopes(org3: Org3) -> None:
    m = org3.admin
    cat = (await m.client.get("/api/v1/apps/scopes")).json()
    keys = {s["key"] for s in cat}
    assert {"contacts:read", "imports:write", "skills:read", "candidates:read"} <= keys
    resp = await m.client.get("/api/v1/api-keys/public-scopes", params={"scopes": "candidate:read,contact:read"})
    assert resp.status_code == 200
    assert "candidates:read" in resp.json() and "contacts:read" in resp.json()
    assert not any(k.startswith("pii.") for k in resp.json())
    async with platform_session() as pdb:
        assert (await pdb.execute(select(App).where(App.owner_tenant_id == org3.tenant_id))).first() is None
