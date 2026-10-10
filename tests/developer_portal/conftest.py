# ruff: noqa: E501
"""Epic 74 test helpers: a signed-up, verified, MFA-enrolled developer (through the HTTP API), an org, a ready sandbox
with a partner App installed, and a platform reviewer."""

import dataclasses
import re
import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pyotp
import pytest
from fastapi import FastAPI
from sqlalchemy import update

from app.core.database import platform_session
from app.core.security import CSRF_COOKIE, CSRF_HEADER
from app.core.settings import get_settings
from app.models.oauth import PartnerOrg
from app.services import vault_service
from app.services.developer_portal import sandboxes
from app.services.developer_portal.conformance import runner
from app.services.marketplace import conformance_report
from tests.epic12 import _isolated_files, _privacy_env, fake_ai, mfa_operator, sent  # noqa: F401
from tests.modules.identity.conftest import new_client

DEV = "/api/v1/developer"
PASSWORD = "Tangerine-Voyage-42"
SIGNING_KEY = "test-conformance-signing-key-e74"
REASON = "Reads candidates to sync them into the partner product."


@dataclasses.dataclass
class Dev:
    client: httpx.AsyncClient
    email: str
    secret: str
    account_id: uuid.UUID
    org_id: uuid.UUID | None = None

    def h(self, org: uuid.UUID | None = None) -> dict[str, str]:
        out = {CSRF_HEADER: self.client.cookies.get(CSRF_COOKIE) or ""}
        if org or self.org_id:
            out["X-Partner-Org"] = str(org or self.org_id)
        return out

    def code(self, offset: int = 0) -> str:
        totp = pyotp.TOTP(self.secret)
        import time

        return totp.at(int(time.time()) + offset * 30)


def email_token(kind: str, to: str) -> str:
    msgs = sent(kind, to)
    assert msgs, f"no {kind} email to {to}"
    m = re.search(r"#t=([A-Za-z0-9_\-]+)", msgs[-1].text)
    assert m is not None
    return m.group(1)


async def signup_verified(app: FastAPI, email: str | None = None, name: str = "Dana Developer") -> tuple[httpx.AsyncClient, str]:
    email = email or f"dev-{uuid.uuid4().hex[:10]}@partner.example.com"
    client = new_client(app)
    resp = await client.post(f"{DEV}/auth/signup", json={"email": email, "name": name, "password": PASSWORD, "accept_terms_version": get_settings().developer_terms_version})
    assert resp.status_code == 202, resp.text
    resp = await client.post(f"{DEV}/auth/verify-email", json={"token": email_token("developer_verify_email", email)})
    assert resp.status_code == 204, resp.text
    return client, email


async def make_dev(app: FastAPI, email: str | None = None, name: str = "Dana Developer") -> Dev:
    client, email = await signup_verified(app, email, name)
    resp = await client.post(f"{DEV}/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200 and resp.json()["step"] == "enroll_required", resp.text
    h = {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}
    resp = await client.post(f"{DEV}/auth/mfa/enroll/start", headers=h)
    assert resp.status_code == 200, resp.text
    secret = resp.json()["secret_display"].replace(" ", "")
    resp = await client.post(f"{DEV}/auth/mfa/enroll/confirm", json={"code": pyotp.TOTP(secret).now()}, headers=h)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["recovery_codes"]) == 10
    me = (await client.get(f"{DEV}/me")).json()
    return Dev(client=client, email=email, secret=secret, account_id=uuid.UUID(me["id"]))


async def make_org(dev: Dev, *, verified: bool = False) -> uuid.UUID:
    slug = f"acme-{secrets.token_hex(4)}"
    resp = await dev.client.post(f"{DEV}/orgs", json={"name": "Acme Integrations", "slug": slug}, headers=dev.h())
    assert resp.status_code == 201, resp.text
    org_id = uuid.UUID(resp.json()["id"])
    dev.org_id = org_id
    if verified:
        async with platform_session() as db:
            await db.execute(update(PartnerOrg).where(PartnerOrg.id == org_id).values(verification_status="verified"))
    return org_id


@pytest.fixture
async def dev(app: FastAPI) -> AsyncIterator[Dev]:
    d = await make_dev(app)
    yield d
    await d.client.aclose()


@pytest.fixture(autouse=True)
def _inline_work() -> Any:
    """Sandbox work runs inline and conformance finalisation is called explicitly."""
    kick, fin = sandboxes._kicker["fn"], runner._kicker["fn"]
    sandboxes.set_kicker(None)
    runner.set_finalize_kicker(None)
    yield
    sandboxes.set_kicker(kick)
    runner.set_finalize_kicker(fin)


async def signing_key() -> None:
    await vault_service.platform_put(conformance_report.SIGNING_SECRET, SIGNING_KEY)


async def ready_sandbox(dev: Dev, name: str = "Main sandbox") -> dict[str, Any]:
    resp = await dev.client.post(f"{DEV}/orgs/{dev.org_id}/sandboxes", json={"name": name, "size": "small"}, headers=dev.h())
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["status"] == "ready", out
    return out


async def partner_app(dev: Dev, *, scopes: tuple[str, ...] = ("candidates:read",), grants: tuple[str, ...] = ("client_credentials",)) -> dict[str, Any]:
    body: dict[str, Any] = {"name": "Acme ATS Sync", "client_type": "confidential", "grant_types": list(grants), "requested_scopes": list(scopes),
                            "scope_reasons": dict.fromkeys(scopes, REASON), "resources": ["api"]}  # fmt: skip
    if "authorization_code" in grants:
        body["redirect_uris"] = ["https://partner.example.com/callback"]
    resp = await dev.client.post(f"{DEV}/orgs/{dev.org_id}/apps", json=body, headers=dev.h())
    assert resp.status_code == 201, resp.text
    return resp.json()


async def install(dev: Dev, sandbox_id: str, app_id: str, scopes: list[str]) -> dict[str, Any]:
    resp = await dev.client.post(f"{DEV}/sandboxes/{sandbox_id}/installations", json={"app_id": app_id, "scopes": scopes}, headers=dev.h())
    assert resp.status_code == 201, resp.text
    return resp.json()


async def playground_token(dev: Dev, sandbox_id: str, app: dict[str, Any]) -> str:
    resp = await dev.client.post(f"{DEV}/playground/token", json={"app_id": app["id"], "sandbox_id": sandbox_id, "client_secret": app["client_secret"]}, headers=dev.h())
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


@pytest.fixture
def fast_sandboxes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provision sandbox tenants without the synthetic seed (the 66 seed runs every public event inline in eager mode)."""
    from sqlalchemy import text

    from app.core.context import RequestContext
    from app.core.permissions import Principal
    from app.core.tenancy import clear_sandbox_cache
    from app.schemas.identity import TenantCreate
    from app.services import auth_service

    async def fast(row: Any) -> uuid.UUID:
        ctx = RequestContext(principal=Principal(type="platform", id=uuid.uuid4()))
        t = await auth_service.create_tenant(ctx, TenantCreate(name=f"Sandbox {row.name}", slug=f"sbx-t-{secrets.token_hex(4)}", seat_limit=10, admin_email="sandbox-admin@example.com"))
        async with platform_session() as db:
            await db.execute(text("UPDATE tenants SET mode='sandbox', sandbox_owner_kind=:k, sandbox_owner_id=:o, rate_tier='sandbox' WHERE id=:t"),
                             {"k": row.owner_kind, "o": row.partner_org_id or row.owner_tenant_id, "t": t.id})  # fmt: skip
        clear_sandbox_cache(t.id)
        return t.id

    monkeypatch.setattr(sandboxes, "_new_tenant", fast)
