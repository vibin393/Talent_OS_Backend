# ruff: noqa: E501
"""Epic 67 (story 67.4): RFC 7009 revocation, RFC 7662 introspection (own tokens only), RFC 8414 metadata."""

from fastapi import FastAPI

from tests.epic3 import Org3
from tests.modules.identity.conftest import new_client
from tests.oauth.e67 import client_credentials, install, public_get, register


async def _post(app: FastAPI, path: str, data: dict[str, str], created: dict) -> dict:
    async with new_client(app) as c:
        resp = await c.post(f"/api/oauth/{path}", data=data, auth=(created["client_id"], created["client_secret"]))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_revoke_and_introspect(app: FastAPI, org3: Org3) -> None:
    a = await register(org3.admin, scopes=("candidates:read",), grant_types=("client_credentials",), redirect_uris=(), name="App A")
    b = await register(org3.admin, scopes=("candidates:read",), grant_types=("client_credentials",), redirect_uris=(), name="App B")
    await install(org3, a)
    await install(org3, b)
    tok_a = (await client_credentials(app, org3, a)).json()["access_token"]
    own = await _post(app, "introspect", {"token": tok_a}, a)
    assert own["active"] is True and own["client_id"] == a["client_id"] and own["token_type"] == "access_token"
    assert own["tenant"] == org3.tenant.slug and own["aud"].endswith("/public/v1")
    assert (await _post(app, "introspect", {"token": tok_a}, b)) == {"active": False}  # foreign
    assert (await _post(app, "introspect", {"token": "tos_at_unknown"}, a)) == {"active": False}
    # foreign revoke: 200 and no effect
    assert await _post(app, "revoke", {"token": tok_a}, b) == {}
    assert (await public_get(app, tok_a)).status_code == 200
    assert await _post(app, "revoke", {"token": tok_a}, a) == {}
    assert (await public_get(app, tok_a)).status_code == 401
    assert (await _post(app, "introspect", {"token": tok_a}, a)) == {"active": False}


async def test_metadata(app: FastAPI) -> None:
    async with new_client(app) as c:
        resp = await c.get("/.well-known/oauth-authorization-server")
    body = resp.json()
    assert "registration_endpoint" not in body
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert "password" not in body["grant_types_supported"]
    assert "pii.contact:read" not in body["scopes_supported"] and "offline_access" in body["scopes_supported"]
    assert "max-age=3600" in resp.headers["cache-control"]
