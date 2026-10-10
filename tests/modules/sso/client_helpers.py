# ruff: noqa: E501
"""Epic 50 test helpers: client IdPs through the admin API, Client Users with a chosen email, and a Client Portal
SSO round trip against the Epic 14 fake OIDC provider / signed SAML responses."""

import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select

from app.core.audit import AuditLog
from app.core.database import tenant_session
from app.models.clients import Contact
from app.models.external_auth import ClientUser, VendorUser
from app.models.sso import ClientUserIdentity
from tests.modules.identity.conftest import Member
from tests.modules.sso.conftest import (
    API,
    CLIENT_ID,
    DOMAIN,
    ISSUER,
    FakeIdp,
    SamlIdp,
    relay_state,
    saml_request_id,
    saml_response,
)


async def client_user(
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    email: str,
    *,
    status: str = "active",
    mfa_enforced: bool = False,
    permissions: tuple[str, ...] = ("submissions:review",),
) -> uuid.UUID:
    async with tenant_session(tenant_id) as db:
        contact = Contact(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            client_id=client_id,
            name="Cora Client",
            email=email,
            type="other",
        )
        db.add(contact)
        await db.flush()
        cu = ClientUser(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            client_id=client_id,
            contact_id=contact.id,
            email=email,
            display_name="Cora Client",
            status=status,
            portal_permissions=list(permissions),
            mfa_enforced=mfa_enforced,
        )
        db.add(cu)
        return cu.id


async def vendor_user(tenant_id: uuid.UUID, vendor_id: uuid.UUID, email: str) -> uuid.UUID:
    async with tenant_session(tenant_id) as db:
        vu = VendorUser(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            vendor_id=vendor_id,
            email=email,
            display_name="Vik Vendor",
            status="active",
            portal_permissions=[],
        )
        db.add(vu)
        return vu.id


def email_at(local: str | None = None, domain: str = DOMAIN) -> str:
    return f"{local or 'p-' + uuid.uuid4().hex[:8]}@{domain}"


async def create_client_oidc(
    admin: Member, client_id: uuid.UUID, *, activate: bool = True, **extra: Any
) -> dict[str, Any]:
    body = {
        "name": f"Client Okta {uuid.uuid4().hex[:6]}",
        "kind": "oidc",
        "email_domains": [DOMAIN],
        "oidc": {"issuer": ISSUER, "client_id": CLIENT_ID},
        "client_secret": "s3cret",
        **extra,
    }
    resp = await admin.client.post(
        f"{API}/clients/{client_id}/sso/identity-providers", json=body, headers=admin.h()
    )
    assert resp.status_code == 201, resp.text
    idp = dict(resp.json())
    if activate:
        act = await admin.client.patch(
            f"{API}/clients/{client_id}/sso/identity-providers/{idp['id']}",
            json={"status": "active"},
            headers=admin.h(),
        )
        assert act.status_code == 200, act.text
        idp = dict(act.json())
    return idp


async def client_oidc_sign_in(
    app: FastAPI,
    fake: FakeIdp,
    slug: str,
    idp_id: str,
    claims: dict[str, Any],
    *,
    next_path: str = "/client/approvals",
    test: bool = False,
) -> tuple[httpx.AsyncClient, httpx.Response]:
    """Client Portal start -> fake IdP -> shared OIDC callback in a fresh browser."""
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    params: dict[str, Any] = {"tenant": slug, "next": next_path}
    if test:
        params["test"] = "1"
    start = await browser.get(f"{API}/client-portal/auth/sso/{idp_id}/start", params=params)
    assert start.status_code == 302, start.text
    query = parse_qs(urlsplit(start.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    fake.claims = claims
    callback = await browser.get(
        f"{API}/auth/sso/oidc/callback",
        params={"code": query["nonce"][0], "state": query["state"][0]},
        headers={"Accept": "application/json"},
    )
    return browser, callback


async def client_saml_sign_in(
    app: FastAPI, slug: str, idp: dict[str, Any], saml: SamlIdp, email: str
) -> tuple[httpx.AsyncClient, httpx.Response]:
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    start = await browser.get(f"{API}/client-portal/auth/sso/{idp['id']}/start", params={"tenant": slug})
    assert start.status_code == 302, start.text
    location = start.headers["location"]
    payload = saml_response(
        saml,
        acs=str(idp["acs_url"]),
        audience=str(idp["sp_entity_id"]),
        request_id=saml_request_id(location),
        email=email,
    )
    resp = await browser.post(
        f"{API}/auth/sso/saml/{idp['id']}/acs",
        data={"SAMLResponse": payload, "RelayState": relay_state(location)},
        headers={"Accept": "application/json"},
    )
    return browser, resp


async def identity_count(tenant_id: uuid.UUID) -> int:
    async with tenant_session(tenant_id) as db:
        return int((await db.execute(select(func.count(ClientUserIdentity.id)))).scalar_one())


async def client_user_count(tenant_id: uuid.UUID) -> int:
    async with tenant_session(tenant_id) as db:
        return int((await db.execute(select(func.count(ClientUser.id)))).scalar_one())


async def audits(tenant_id: uuid.UUID, action: str) -> list[dict[str, Any]]:
    async with tenant_session(tenant_id) as db:
        rows = (
            await db.execute(
                select(AuditLog.data).where(AuditLog.action == action).order_by(AuditLog.occurred_at)
            )
        ).scalars()
        return [dict(r or {}) for r in rows]
