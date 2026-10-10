# ruff: noqa: E501, F811
"""Epic 50 (AD-48, NFR-41): cross-Client and cross-principal negative suite for Client Portal SSO.

An assertion from Client A's IdP must never create a session, an identity link or a JIT Client User for anyone
but a Client User of A within the IdP's domains: not a Client User of B with the same email, an internal User, a
Vendor User, a Candidate, or a Client User of A outside the domain list. The reverse: an internal IdP assertion
for a Client User's email resolves internally only (the Client User is untouched).
"""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.external_auth import ClientUser
from app.models.sso import ClientUserIdentity, UserIdentity
from tests.modules.demands.helpers import create_client
from tests.modules.identity.conftest import Org, join
from tests.modules.offers.helpers import create_candidate
from tests.modules.sso.client_helpers import (
    audits,
    client_oidc_sign_in,
    client_user,
    client_user_count,
    create_client_oidc,
    email_at,
    identity_count,
    vendor_user,
)
from tests.modules.sso.conftest import (  # noqa: F401
    API,
    FakeIdp,
    activate,
    create_oidc_idp,
    fake_idp,
    oidc_sign_in,
    sso_org,
)
from tests.p2 import make_vendor

TARGETS = (
    "client_user_of_b",
    "internal_user",
    "vendor_user",
    "candidate",
    "client_user_of_a_outside_domains",
)
EXPECTED_REASON = {
    "client_user_of_b": "other_client",
    "internal_user": "other_principal",
    "vendor_user": "other_principal",
    "candidate": "no_client_user",
    "client_user_of_a_outside_domains": "domain_not_allowed",
}


async def _target(app: FastAPI, org: Org, kind: str, client_a: uuid.UUID, client_b: uuid.UUID) -> str:
    tid = org.tenant.id
    if kind == "client_user_of_b":
        email = email_at()
        await client_user(tid, client_b, email)
    elif kind == "internal_user":
        member = await join(app, org.tenant, "recruiter")  # initech.example.com == the IdP's domain
        await member.client.aclose()
        email = member.email
    elif kind == "vendor_user":
        email = email_at()
        await vendor_user(tid, await make_vendor(tid), email)
    elif kind == "candidate":
        email = email_at()
        await create_candidate(org.admin, email=email)
    else:
        email = email_at(domain="acme-outside.example.org")
        await client_user(tid, client_a, email)
    return email


@pytest.mark.parametrize("jit", [False, True], ids=["jit_off", "jit_on"])
@pytest.mark.parametrize("kind", TARGETS)
async def test_client_a_idp_never_signs_in_another_principal(
    app: FastAPI,
    fake_idp: FakeIdp,
    sso_org: Org,
    kind: str,
    jit: bool,
) -> None:
    if jit and kind == "candidate":
        pytest.skip("JIT for a candidate's email creates a Client User of A only (covered by the JIT test)")
    client_a = await create_client(sso_org.admin)
    client_b = await create_client(sso_org.admin)
    idp = await create_client_oidc(sso_org.admin, client_a, jit_enabled=jit)
    email = await _target(app, sso_org, kind, client_a, client_b)
    users_before = await client_user_count(sso_org.tenant.id)
    claims: dict[str, Any] = {"sub": f"sub-{uuid.uuid4().hex[:8]}", "email": email, "email_verified": True}
    browser, cb = await client_oidc_sign_in(app, fake_idp, sso_org.tenant.slug, idp["id"], claims)
    try:
        assert cb.status_code == 401, cb.text
        expected_code = (
            "sso_domain_not_allowed" if kind == "client_user_of_a_outside_domains" else "sso_no_client_user"
        )
        assert cb.json()["error"]["code"] == expected_code
        set_cookie = cb.headers.get("set-cookie", "")
        for cookie in ("tos_client=", "tos_session=", "tos_vendor=", "tos_candidate=", "tos_mfa="):
            assert cookie not in set_cookie
        assert (await browser.get(f"{API}/client-portal/me")).status_code == 401
        assert (await browser.get(f"{API}/auth/me")).status_code == 401
    finally:
        await browser.aclose()
    assert await identity_count(sso_org.tenant.id) == 0
    assert await client_user_count(sso_org.tenant.id) == users_before
    refusals = await audits(sso_org.tenant.id, "client_sso.login_refused")
    assert refusals[-1]["reason"] == EXPECTED_REASON[kind]


async def test_linked_subject_of_another_client_is_refused(
    app: FastAPI, fake_idp: FakeIdp, sso_org: Org
) -> None:
    """Defence in depth: a link row pointing at a Client User of B (corrupted data) never signs them in."""
    client_a = await create_client(sso_org.admin)
    client_b = await create_client(sso_org.admin)
    idp = await create_client_oidc(sso_org.admin, client_a)
    cu_b = await client_user(sso_org.tenant.id, client_b, email_at())
    async with tenant_session(sso_org.tenant.id) as db:
        db.add(
            ClientUserIdentity(
                id=uuid.uuid4(),
                tenant_id=sso_org.tenant.id,
                client_user_id=cu_b,
                idp_id=uuid.UUID(idp["id"]),
                subject="evil",
            )
        )
    browser, cb = await client_oidc_sign_in(
        app,
        fake_idp,
        sso_org.tenant.slug,
        idp["id"],
        {"sub": "evil", "email": email_at(), "email_verified": True},
    )
    await browser.aclose()
    assert cb.status_code == 401 and cb.json()["error"]["code"] == "sso_no_client_user"
    assert (await audits(sso_org.tenant.id, "client_sso.login_refused"))[-1]["reason"] == "other_client"


async def test_internal_idp_never_touches_client_users(app: FastAPI, fake_idp: FakeIdp, sso_org: Org) -> None:
    client_a = await create_client(sso_org.admin)
    email = email_at()
    cu_id = await client_user(sso_org.tenant.id, client_a, email)
    internal = await create_oidc_idp(sso_org.admin)
    assert (await activate(sso_org.admin, internal["id"])).status_code == 200
    browser, cb = await oidc_sign_in(
        app,
        fake_idp,
        sso_org.tenant.slug,
        internal["id"],
        {"sub": "int-1", "email": email, "email_verified": True},
    )
    try:
        assert cb.status_code == 403 and cb.json()["error"]["code"] == "sso_no_account"
        assert (await browser.get(f"{API}/client-portal/me")).status_code == 401
    finally:
        await browser.aclose()
    async with tenant_session(sso_org.tenant.id) as db:
        cu = await db.get(ClientUser, cu_id)
        assert cu is not None and cu.status == "active" and cu.last_sign_in_at is None
        assert (await db.execute(select(ClientUserIdentity.id))).first() is None
        assert (await db.execute(select(UserIdentity.id))).first() is None


async def test_client_idp_of_one_tenant_is_invisible_to_another(
    app: FastAPI, fake_idp: FakeIdp, sso_org: Org
) -> None:
    from tests.modules.identity.conftest import make_tenant

    client_a = await create_client(sso_org.admin)
    idp = await create_client_oidc(sso_org.admin, client_a)
    other = await make_tenant(seat_limit=5)
    browser = __import__("httpx").AsyncClient(
        transport=__import__("httpx").ASGITransport(app=app), base_url="http://test"
    )
    try:
        start = await browser.get(
            f"{API}/client-portal/auth/sso/{idp['id']}/start",
            params={"tenant": other.slug},
            headers={"Accept": "application/json"},
        )
        assert start.status_code == 404
        disc = await browser.post(
            f"{API}/client-portal/auth/sso/discover", json={"tenant_slug": other.slug, "email": email_at()}
        )
        assert disc.json() == {"sso": False, "idp_id": None, "required": False}
    finally:
        await browser.aclose()
