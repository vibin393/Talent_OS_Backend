# ruff: noqa: E501
"""Epic 67 test helpers: register a private App through the API, install it (``tests/support/oauth.install_app``),
drive the token endpoint and the authorize / consent flow."""

import base64
import hashlib
import secrets
import uuid
from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from fastapi import FastAPI

from app.core.database import platform_session
from app.models.oauth import App, AppInstallation
from tests.epic3 import Org3, h
from tests.modules.identity.conftest import Member, new_client
from tests.support.oauth import install_app

REDIRECT = "https://client.example.test/callback"
TOKEN = "/api/oauth/token"


def reasons(scopes: Sequence[str]) -> dict[str, str]:
    return {k: f"The integration needs {k} to work" for k in scopes}


async def register(
    m: Member,
    *,
    scopes: Sequence[str] = ("candidates:read", "pii.contact:read", "offline_access"),
    client_type: str = "confidential",
    grant_types: Sequence[str] = ("client_credentials", "authorization_code"),
    resources: Sequence[str] = ("api",),
    redirect_uris: Sequence[str] = (REDIRECT,),
    name: str = "Acme Sync",
) -> dict[str, Any]:
    body = {
        "name": name, "client_type": client_type, "grant_types": list(grant_types), "redirect_uris": list(redirect_uris),
        "resources": list(resources), "requested_scopes": list(scopes), "scope_reasons": reasons(scopes),
        "description": "Syncs candidates", "manifest": {"data_flows": [{"direction": "out", "entity": "candidate", "fields": ["name"], "purpose": "Sync", "destination_country": "IN"}]},
    }  # fmt: skip
    resp = await m.client.post("/api/v1/apps", json=body, headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def install(org: Org3, created: dict[str, Any], scopes: Sequence[str] | None = None) -> AppInstallation:
    async with platform_session() as pdb:
        app = await pdb.get(App, uuid.UUID(created["id"]))
    assert app is not None
    return await install_app(org.tenant_id, app, scopes=scopes, granted_by=org.admin.user_id)


async def token_call(app: FastAPI, data: dict[str, str], auth: tuple[str, str | None] | None = None) -> httpx.Response:
    headers = {}
    if auth is not None and auth[1] is not None:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    elif auth is not None:
        data = {**data, "client_id": auth[0]}
    async with new_client(app) as client:
        return await client.post(TOKEN, data=data, headers=headers)


async def client_credentials(app: FastAPI, org: Org3, created: dict[str, Any], scope: str | None = None) -> httpx.Response:
    data = {"grant_type": "client_credentials", "tenant": org.tenant.slug}
    if scope is not None:
        data["scope"] = scope
    return await token_call(app, data, (created["client_id"], created["client_secret"]))


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)[:64]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_query(created: dict[str, Any], challenge: str, *, scope: str = "candidates:read offline_access", **extra: str) -> str:
    q = {"client_id": created["client_id"], "redirect_uri": REDIRECT, "response_type": "code", "scope": scope,
         "state": "xyz", "code_challenge": challenge, "code_challenge_method": "S256", **extra}  # fmt: skip
    return "/api/oauth/authorize/view?" + urlencode(q)


async def consent(m: Member, created: dict[str, Any], *, scope: str = "candidates:read offline_access") -> tuple[str, str]:
    """Run view + approve; returns (code, verifier)."""
    verifier, challenge = pkce()
    view = await m.client.get(authorize_query(created, challenge, scope=scope))
    assert view.status_code == 200, view.text
    body = view.json()
    assert body.get("state") == "ok", body
    resp = await m.client.post("/api/oauth/authorize/decision", json={"request_id": body["request_id"], "approve": True}, headers=h(m))
    assert resp.status_code == 200, resp.text
    params = parse_qs(urlsplit(resp.json()["redirect_to"]).query)
    assert params["state"] == ["xyz"]
    return params["code"][0], verifier


async def exchange(app: FastAPI, created: dict[str, Any], code: str, verifier: str) -> httpx.Response:
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier}
    return await token_call(app, data, (created["client_id"], created.get("client_secret")))


async def refresh_call(app: FastAPI, created: dict[str, Any], rt: str, scope: str | None = None) -> httpx.Response:
    data = {"grant_type": "refresh_token", "refresh_token": rt}
    if scope:
        data["scope"] = scope
    return await token_call(app, data, (created["client_id"], created.get("client_secret")))


async def public_get(app: FastAPI, token: str, path: str = "/api/public/v1/meta") -> httpx.Response:
    async with new_client(app) as client:
        return await client.get(path, headers={"Authorization": f"Bearer {token}"})
