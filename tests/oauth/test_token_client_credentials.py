# ruff: noqa: E501
"""Epic 67 (story 67.2): the token endpoint with client credentials (spec 0.2 / 0.3)."""

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update

from app.core import rate_limit
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.permissions import Principal
from app.core.settings import get_settings
from app.models.oauth import App
from app.services import ecosystem_service
from tests.epic3 import Org3
from tests.oauth.e67 import client_credentials, install, public_get, register, token_call

SCOPES = ("candidates:read", "demands:read", "pii.contact:read")


async def test_happy_path_default_scopes_exclude_sensitive(app: FastAPI, org3: Org3) -> None:
    created = await register(org3.admin, scopes=SCOPES, grant_types=("client_credentials",), redirect_uris=())
    await install(org3, created)
    resp = await client_credentials(app, org3, created)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600 and "refresh_token" not in body
    assert body["scope"] == "candidates:read demands:read"  # D-7: never the sensitive scope
    assert resp.headers["cache-control"] == "no-store"
    me = await public_get(app, body["access_token"])
    assert me.status_code == 200 and me.json()["principal"]["user_id"] is None
    narrow = await client_credentials(app, org3, created, scope="candidates:read")
    assert narrow.json()["scope"] == "candidates:read"


async def test_errors(app: FastAPI, org3: Org3, other3: Org3) -> None:
    created = await register(org3.admin, scopes=SCOPES, grant_types=("client_credentials",), redirect_uris=())
    # not installed
    resp = await client_credentials(app, org3, created)
    assert (resp.status_code, resp.json()["error"]) == (401, "invalid_client")
    assert resp.headers["www-authenticate"].startswith("Basic")
    await install(org3, created)
    # tenant required
    resp = await token_call(app, {"grant_type": "client_credentials"}, (created["client_id"], created["client_secret"]))
    assert resp.json()["error"] == "invalid_request"
    # a private App in another tenant is not installed there
    resp = await client_credentials(app, other3, created)
    assert resp.json()["error"] == "invalid_client"
    # sensitive scope / mcp resource
    resp = await client_credentials(app, org3, created, scope="pii.contact:read")
    assert resp.json()["error"] == "invalid_scope"
    data = {"grant_type": "client_credentials", "tenant": org3.tenant.slug, "resource": get_settings().p5_mcp_resource_url()}
    resp = await token_call(app, data, (created["client_id"], created["client_secret"]))
    assert resp.json()["error"] == "invalid_target"
    # bad secret, unsupported grants, secret in query
    resp = await client_credentials(app, org3, {**created, "client_secret": "tos_cs_wrong"})
    assert resp.status_code == 401
    resp = await token_call(app, {"grant_type": "password", "username": "a", "password": "b"}, (created["client_id"], created["client_secret"]))
    assert resp.json()["error"] == "unsupported_grant_type"
    from tests.modules.identity.conftest import new_client

    async with new_client(app) as c:
        resp = await c.post(f"/api/oauth/token?client_secret={created['client_secret']}", data={"grant_type": "client_credentials"})
    assert resp.json()["error"] == "invalid_request"


async def test_suspended_app_and_kill_switch(app: FastAPI, org3: Org3) -> None:
    created = await register(org3.admin, scopes=("candidates:read",), grant_types=("client_credentials",), redirect_uris=())
    await install(org3, created)
    human = RequestContext(tenant_id=org3.tenant_id, principal=Principal(type="user", id=org3.admin.user_id, tenant_id=org3.tenant_id))
    async with tenant_session(org3.tenant_id) as db:
        await ecosystem_service.set_kill_switch(human, db, on=True, reason="incident")
    resp = await client_credentials(app, org3, created)
    assert (resp.status_code, resp.json()["error"]) == (403, "ecosystem_suspended")
    async with tenant_session(org3.tenant_id) as db:
        await ecosystem_service.set_kill_switch(human, db, on=False, reason="ok")
    async with platform_session() as pdb:
        await pdb.execute(update(App).where(App.client_id == created["client_id"]).values(status="suspended"))
    ecosystem_service.bust_cache()
    resp = await client_credentials(app, org3, created)
    assert (resp.status_code, resp.json()["error"]) == (401, "invalid_client")


async def test_failure_audit_is_sampled(app: FastAPI, org3: Org3) -> None:
    from app.core.audit import AuditLog

    created = await register(org3.admin, scopes=("candidates:read",), grant_types=("client_credentials",), redirect_uris=())
    for _ in range(3):
        await client_credentials(app, org3, {**created, "client_secret": "tos_cs_wrong"})
    async with platform_session() as pdb:
        count = (
            await pdb.execute(
                select(func.count()).select_from(AuditLog).where(
                    AuditLog.action == "oauth.client_auth_failed", AuditLog.data["client_id"].astext == created["client_id"]
                )
            )
        ).scalar_one()
    assert count == 1


async def test_oauth_token_rate_limit(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    get_settings.cache_clear()
    rate_limit.set_backend(rate_limit.MemoryBackend())
    try:
        statuses = [(await token_call(app, {"grant_type": "client_credentials"}, ("tosapp_x", "y"))).status_code for _ in range(62)]
    finally:
        rate_limit.set_backend(None)
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
        get_settings.cache_clear()
    assert statuses[-1] == 429 and statuses[0] == 401
