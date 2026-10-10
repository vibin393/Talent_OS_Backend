# ruff: noqa: E501
"""Epic 67 (story 67.4, NFR-61): uninstall, suspension, kill switch, user deactivation, grant reduction and authorisation
revoke each fail the next call (the 60 s principal cache collapsed by the revocation paths / a frozen-clock bust)."""

import uuid

from fastapi import FastAPI
from sqlalchemy import update

from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.permissions import Principal
from app.models.identity import User
from app.models.oauth import App, AppInstallation
from app.services import ecosystem_service, oauth_service
from tests.epic3 import Org3, h
from tests.oauth.e67 import consent, exchange, install, public_get, register


def _expire_cache() -> None:  # the 60 s window, elapsed
    oauth_service.clear_cache()
    ecosystem_service.bust_cache()


async def _delegated(app: FastAPI, org: Org3) -> tuple[dict, AppInstallation, str]:
    created = await register(org.admin, scopes=("candidates:read", "demands:read"))
    inst = await install(org, created)
    code, verifier = await consent(org.admin, created, scope="candidates:read demands:read")
    token = (await exchange(app, created, code, verifier)).json()["access_token"]
    assert (await public_get(app, token)).status_code == 200
    return created, inst, token


async def test_uninstall(app: FastAPI, org3: Org3) -> None:
    _, inst, token = await _delegated(app, org3)
    async with tenant_session(org3.tenant_id) as db:
        await db.execute(update(AppInstallation).where(AppInstallation.id == inst.id).values(status="uninstalled"))
    _expire_cache()
    assert (await public_get(app, token)).status_code == 401


async def test_app_suspension(app: FastAPI, org3: Org3) -> None:
    created, _, token = await _delegated(app, org3)
    async with platform_session() as pdb:
        await pdb.execute(update(App).where(App.id == uuid.UUID(created["id"])).values(status="suspended"))
    _expire_cache()
    assert (await public_get(app, token)).status_code == 401


async def test_kill_switch(app: FastAPI, org3: Org3) -> None:
    _, _, token = await _delegated(app, org3)
    human = RequestContext(tenant_id=org3.tenant_id, principal=Principal(type="user", id=org3.admin.user_id, tenant_id=org3.tenant_id))
    async with tenant_session(org3.tenant_id) as db:
        await ecosystem_service.set_kill_switch(human, db, on=True, reason="incident")
    resp = await public_get(app, token)
    assert (resp.status_code, resp.json()["error"]["code"]) == (403, "ecosystem_suspended")


async def test_user_deactivation(app: FastAPI, org3: Org3) -> None:
    _, _, token = await _delegated(app, org3)
    async with tenant_session(org3.tenant_id) as db:
        await db.execute(update(User).where(User.id == org3.admin.user_id).values(is_active=False))
    _expire_cache()
    assert (await public_get(app, token)).status_code == 401


async def test_grant_reduction(app: FastAPI, org3: Org3) -> None:
    _, inst, token = await _delegated(app, org3)
    async with tenant_session(org3.tenant_id) as db:
        await db.execute(update(AppInstallation).where(AppInstallation.id == inst.id).values(granted_scopes=["demands:read"]))
    _expire_cache()
    resp = await public_get(app, token)
    assert resp.status_code == 200 and resp.json()["scopes"] == ["demands:read"]


async def test_authorization_revoke_from_profile(app: FastAPI, org3: Org3) -> None:
    _, _, token = await _delegated(app, org3)
    m = org3.admin
    rows = (await m.client.get("/api/v1/me/connected-apps")).json()
    assert len(rows) == 1 and rows[0]["app_name"] == "Acme Sync" and rows[0]["resource"] == "api"
    resp = await m.client.delete(f"/api/v1/me/connected-apps/{rows[0]['id']}", headers=h(m))
    assert resp.status_code == 204
    assert (await public_get(app, token)).status_code == 401  # no cache bust needed: revoke_for clears it
    assert (await m.client.get("/api/v1/me/connected-apps")).json() == []
