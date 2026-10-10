# ruff: noqa: E501
"""Epic 67 (story 67.3): authorize + consent + code exchange with PKCE S256 (spec 0.4)."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import Principal
from app.models.oauth import OAuthAuthCode, OAuthToken
from app.schemas.ecosystem import AppPolicyIn
from app.services import ecosystem_service
from tests.epic3 import Org3, h, member
from tests.modules.identity.conftest import new_client
from tests.oauth.e67 import REDIRECT, authorize_query, consent, exchange, install, pkce, public_get, register

SCOPES = ("candidates:read", "pii.contact:read", "offline_access", "demands:read")


async def _setup(org: Org3, grant: tuple[str, ...] | None = None) -> dict:
    created = await register(org.admin, scopes=SCOPES)
    await install(org, created, scopes=grant)
    return created


async def test_pkce_happy_path_with_refresh(app: FastAPI, org3: Org3) -> None:
    created = await _setup(org3)
    code, verifier = await consent(org3.admin, created, scope="candidates:read pii.contact:read offline_access")
    resp = await exchange(app, created, code, verifier)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["refresh_token"].startswith("tos_rt_")
    assert set(body["scope"].split()) == {"candidates:read", "pii.contact:read", "offline_access"}
    me = await public_get(app, body["access_token"])
    assert me.status_code == 200 and me.json()["principal"]["user_id"] == str(org3.admin.user_id)
    # code reuse -> invalid_grant and the issued family is revoked
    again = await exchange(app, created, code, verifier)
    assert again.json()["error"] == "invalid_grant"
    assert (await public_get(app, body["access_token"])).status_code == 401


async def test_query_errors(app: FastAPI, org3: Org3) -> None:
    created = await _setup(org3)
    m = org3.admin
    _, challenge = pkce()
    # plain PKCE -> redirect error
    resp = await m.client.get(authorize_query(created, challenge, code_challenge_method="plain"))
    target = parse_qs(urlsplit(resp.json()["redirect_to"]).query)
    assert target["error"] == ["invalid_request"] and target["state"] == ["xyz"]
    # missing state
    resp = await m.client.get(authorize_query(created, challenge, state=""))
    assert parse_qs(urlsplit(resp.json()["redirect_to"]).query)["error"] == ["invalid_request"]
    # redirect mismatch (trailing slash) -> error page, no redirect; frame headers present
    resp = await m.client.get(authorize_query(created, challenge, redirect_uri=REDIRECT + "/"))
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_redirect_uri"
    assert "redirect_to" not in resp.text
    resp = await m.client.get(authorize_query(created, challenge))
    assert resp.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    # no session -> 401
    async with new_client(app) as c:
        assert (await c.get(authorize_query(created, challenge))).status_code == 401


async def test_scope_downgrade_and_not_held(app: FastAPI, org3: Org3) -> None:
    created = await _setup(org3, grant=("candidates:read", "offline_access", "demands:read"))
    rita = await member(app, org3, "recruiter")
    _, challenge = pkce()
    view = (await org3.admin.client.get(authorize_query(created, challenge, scope="candidates:read pii.contact:read"))).json()
    assert view["scopes"] == ["candidates:read"]
    assert [s["key"] for s in view["unavailable_scopes"]] == ["pii.contact:read"]
    # a user who lacks demand:read cannot approve demands:read
    from app.core.database import tenant_session as ts
    from app.services import identity_bridge

    async with ts(org3.tenant_id) as db:
        perms = await identity_bridge.user_permissions(db, rita.user_id)
    view = (await rita.client.get(authorize_query(created, challenge, scope="candidates:read demands:read"))).json()
    if "demand:read" in perms:
        assert "demands:read" in view["scopes"]
    else:
        assert [s["key"] for s in view["not_held_scopes"]] == ["demands:read"]
    # approving a scope outside the approvable list is refused
    resp = await org3.admin.client.post(
        "/api/oauth/authorize/decision",
        json={"request_id": (await org3.admin.client.get(authorize_query(created, challenge, scope="candidates:read"))).json()["request_id"],
              "approve": True, "scopes": ["pii.contact:read"]},
        headers=h(org3.admin),
    )  # fmt: skip
    assert resp.status_code == 422


async def test_view_states(app: FastAPI, org3: Org3) -> None:
    created = await register(org3.admin, scopes=SCOPES, resources=("api", "mcp"))
    _, challenge = pkce()
    m = org3.admin
    view = (await m.client.get(authorize_query(created, challenge))).json()
    assert view["state"] == "app_not_installed" and view["request_id"] == ""
    await install(org3, created)
    human = RequestContext(tenant_id=org3.tenant_id, principal=Principal(type="user", id=m.user_id, tenant_id=org3.tenant_id))
    async with tenant_session(org3.tenant_id) as db:
        await ecosystem_service.set_app_policy(human, db, AppPolicyIn(user_self_authorize=False))
    assert (await m.client.get(authorize_query(created, challenge))).json()["state"] == "needs_admin_approval"
    async with tenant_session(org3.tenant_id) as db:
        await ecosystem_service.set_app_policy(human, db, AppPolicyIn(user_self_authorize=True))
    view = (await m.client.get(authorize_query(created, challenge, scope="mcp:read", resource="mcp"))).json()
    assert view.get("state") == "mcp_disabled", view


async def test_decision_bound_to_user_single_use_and_csrf(app: FastAPI, org3: Org3) -> None:
    created = await _setup(org3)
    other = await member(app, org3, "recruiter", name="Other User")
    _, challenge = pkce()
    view = (await org3.admin.client.get(authorize_query(created, challenge, scope="candidates:read"))).json()
    rid = view["request_id"]
    # CSRF required
    resp = await org3.admin.client.post("/api/oauth/authorize/decision", json={"request_id": rid, "approve": True})
    assert resp.status_code == 403
    # foreign request id (and it is consumed)
    resp = await other.client.post("/api/oauth/authorize/decision", json={"request_id": rid, "approve": True}, headers=h(other))
    assert resp.status_code == 403
    resp = await org3.admin.client.post("/api/oauth/authorize/decision", json={"request_id": rid, "approve": True}, headers=h(org3.admin))
    assert resp.status_code == 403  # single use
    # deny -> access_denied redirect
    view = (await org3.admin.client.get(authorize_query(created, challenge, scope="candidates:read"))).json()
    resp = await org3.admin.client.post("/api/oauth/authorize/decision", json={"request_id": view["request_id"], "approve": False}, headers=h(org3.admin))
    assert parse_qs(urlsplit(resp.json()["redirect_to"]).query)["error"] == ["access_denied"]


async def test_code_expiry_and_wrong_verifier(app: FastAPI, org3: Org3) -> None:
    created = await _setup(org3)
    code, verifier = await consent(org3.admin, created, scope="candidates:read")
    resp = await exchange(app, created, code, verifier[:-1] + ("A" if verifier[-1] != "A" else "B"))
    assert resp.json()["error"] == "invalid_grant"
    code, verifier = await consent(org3.admin, created, scope="candidates:read")
    async with tenant_session(org3.tenant_id) as db:
        await db.execute(update(OAuthAuthCode).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    resp = await exchange(app, created, code, verifier)
    assert resp.json()["error"] == "invalid_grant"
    async with tenant_session(org3.tenant_id) as db:
        assert (await db.execute(select(OAuthToken))).first() is None
