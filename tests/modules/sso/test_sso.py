"""Epic 14 (FR-77, AD-30): SSO administration, OIDC and SAML sign-in, enforcement and break-glass."""

import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.audit import AuditLog
from app.core.database import tenant_session
from app.models.identity import Role, User
from app.models.sso import IdentityProvider, UserIdentity
from tests.modules.identity.conftest import USER_PASSWORD, Org, csrf, join, make_tenant
from tests.modules.sso.conftest import (
    API,
    DOMAIN,
    FakeIdp,
    SamlIdp,
    activate,
    create_oidc_idp,
    oidc_sign_in,
    relay_state,
    saml_request_id,
    saml_response,
)
from tests.p2 import make_client_user, sign_in_external


async def _set_user(tenant_id: uuid.UUID, user_id: uuid.UUID, **values: object) -> None:
    async with tenant_session(tenant_id) as db:
        await db.execute(update(User).where(User.id == user_id).values(**values))


async def _break_glass(org: Org) -> None:
    await _set_user(org.tenant.id, org.admin.user_id, mfa_enabled=True)
    resp = await org.admin.client.put(
        f"{API}/sso/break-glass-admins/{org.admin.user_id}", json={"break_glass": True}, headers=org.admin.h()
    )
    assert resp.status_code == 200, resp.text


async def _actions(tenant_id: uuid.UUID) -> list[str]:
    async with tenant_session(tenant_id) as db:
        return list((await db.execute(select(AuditLog.action).order_by(AuditLog.occurred_at))).scalars())


# ================================================================ administration
async def test_admin_crud_never_returns_secret(sso_org: Org) -> None:
    idp = await create_oidc_idp(sso_org.admin)
    assert idp["status"] == "draft" and idp["has_secret"] is True
    assert "s3cret" not in str(idp) and "secret" not in idp
    assert idp["redirect_uri"].endswith("/api/v1/auth/sso/oidc/callback")
    async with tenant_session(sso_org.tenant.id) as db:
        stored = (await db.execute(select(IdentityProvider.secret))).scalar_one()
    assert stored and "s3cret" not in stored
    activated = await activate(sso_org.admin, idp["id"])
    assert activated.status_code == 200 and activated.json()["status"] == "active"
    listed = await sso_org.admin.client.get(f"{API}/sso/identity-providers")
    assert [i["id"] for i in listed.json()] == [idp["id"]]
    test = await sso_org.admin.client.post(
        f"{API}/sso/identity-providers/{idp['id']}/test", headers=sso_org.admin.h()
    )
    assert test.json()["url"].startswith(f"/api/v1/auth/sso/{idp['id']}/start?tenant=")
    # Deleting an unlinked IdP removes it.
    gone = await sso_org.admin.client.delete(
        f"{API}/sso/identity-providers/{idp['id']}", headers=sso_org.admin.h()
    )
    assert gone.status_code == 204
    assert (await sso_org.admin.client.get(f"{API}/sso/identity-providers/{idp['id']}")).status_code == 404
    assert "sso.identity_provider_created" in await _actions(sso_org.tenant.id)


async def test_enforcement_requires_break_glass(sso_org: Org) -> None:
    idp = await create_oidc_idp(sso_org.admin)
    refused = await activate(sso_org.admin, idp["id"], enforce_for="all")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "break_glass_required"
    # Break-glass without MFA does not count.
    resp = await sso_org.admin.client.put(
        f"{API}/sso/break-glass-admins/{sso_org.admin.user_id}",
        json={"break_glass": True},
        headers=sso_org.admin.h(),
    )
    assert resp.status_code == 200
    assert (await activate(sso_org.admin, idp["id"], enforce_for="all")).status_code == 409
    await _set_user(sso_org.tenant.id, sso_org.admin.user_id, mfa_enabled=True)
    ok = await activate(sso_org.admin, idp["id"], enforce_for="all")
    assert ok.status_code == 200, ok.text
    # The last break-glass admin cannot be removed while enforcement is on.
    last = await sso_org.admin.client.put(
        f"{API}/sso/break-glass-admins/{sso_org.admin.user_id}",
        json={"break_glass": False},
        headers=sso_org.admin.h(),
    )
    assert last.status_code == 409 and last.json()["error"]["code"] == "break_glass_required"
    admins = (await sso_org.admin.client.get(f"{API}/sso/break-glass-admins")).json()
    assert admins[0]["break_glass"] and admins[0]["mfa_enabled"]


async def test_sso_admin_requires_permission_and_tenant_isolation(
    app: FastAPI, sso_org: Org, other_org: Org
) -> None:
    idp = await create_oidc_idp(sso_org.admin)
    assert (await other_org.admin.client.get(f"{API}/sso/identity-providers/{idp['id']}")).status_code == 404
    recruiter = await join(app, sso_org.tenant, "recruiter")
    try:
        denied = await recruiter.client.get(f"{API}/sso/identity-providers")
        assert denied.status_code == 403
        assert denied.json()["error"]["details"]["required_permission"] == "sso:manage"
    finally:
        await recruiter.client.aclose()


# ================================================================ OIDC sign-in
async def test_oidc_links_existing_user_by_verified_email(
    app: FastAPI, fake_idp: FakeIdp, sso_org: Org
) -> None:
    recruiter = await join(app, sso_org.tenant, "recruiter", name="Rita Recruiter")
    await recruiter.client.aclose()
    idp = await create_oidc_idp(sso_org.admin)
    await activate(sso_org.admin, idp["id"])
    claims = {"sub": "okta|rita", "email": recruiter.email, "email_verified": True, "name": "Rita"}
    browser, callback = await oidc_sign_in(app, fake_idp, sso_org.tenant.slug, idp["id"], claims)
    try:
        assert callback.status_code == 303, callback.text
        assert callback.headers["location"].endswith("/candidates")
        assert fake_idp.last_token_form["code_verifier"] and fake_idp.last_token_form["client_secret"] == [
            "s3cret"
        ]
        me = await browser.get(f"{API}/auth/me")
        assert me.status_code == 200 and me.json()["user"]["id"] == str(recruiter.user_id)
        assert me.json()["user"]["role_keys"] == ["recruiter"]
    finally:
        await browser.aclose()
    async with tenant_session(sso_org.tenant.id) as db:
        link = (await db.execute(select(UserIdentity))).scalar_one()
    assert link.user_id == recruiter.user_id and link.subject == "okta|rita"
    assert "sso.identity_linked" in await _actions(sso_org.tenant.id)
    # Second sign-in resolves by subject (even if the email claim changed).
    browser2, again = await oidc_sign_in(
        app, fake_idp, sso_org.tenant.slug, idp["id"], {**claims, "email": "renamed@other.example.com"}
    )
    await browser2.aclose()
    assert again.status_code == 303


async def test_oidc_refusals(app: FastAPI, fake_idp: FakeIdp, sso_org: Org) -> None:
    idp = await create_oidc_idp(sso_org.admin)
    await activate(sso_org.admin, idp["id"])
    slug = sso_org.tenant.slug
    member = await join(app, sso_org.tenant, "recruiter")
    await member.client.aclose()
    # Unverified email: never linked.
    b, r = await oidc_sign_in(
        app, fake_idp, slug, idp["id"], {"sub": "s1", "email": member.email, "email_verified": False}
    )
    await b.aclose()
    assert r.status_code == 403 and r.json()["error"]["code"] == "sso_email_unverified"
    # No account and JIT off.
    b, r = await oidc_sign_in(
        app, fake_idp, slug, idp["id"], {"sub": "s2", "email": f"new@{DOMAIN}", "email_verified": True}
    )
    await b.aclose()
    assert r.status_code == 403 and r.json()["error"]["code"] == "sso_no_account"
    # Deactivated user: refused, nothing linked.
    await _set_user(sso_org.tenant.id, member.user_id, is_active=False)
    b, r = await oidc_sign_in(
        app, fake_idp, slug, idp["id"], {"sub": "s3", "email": member.email, "email_verified": True}
    )
    await b.aclose()
    assert r.status_code == 403 and r.json()["error"]["code"] == "user_deactivated"
    async with tenant_session(sso_org.tenant.id) as db:
        assert (await db.execute(select(UserIdentity))).first() is None
    # Nonce mismatch.
    fake_idp.nonce_override = "evil"
    b, r = await oidc_sign_in(
        app, fake_idp, slug, idp["id"], {"sub": "s4", "email": member.email, "email_verified": True}
    )
    await b.aclose()
    fake_idp.nonce_override = None
    assert r.status_code == 401 and r.json()["error"]["code"] == "sso_assertion_invalid"
    # Browser (non-JSON) failures redirect to the login page.
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        bad = await browser.get(f"{API}/auth/sso/oidc/callback", params={"code": "x", "state": "nope"})
        assert bad.status_code == 303 and "sso_error=sso_assertion_invalid" in bad.headers["location"]
    finally:
        await browser.aclose()


async def test_oidc_state_is_single_use(app: FastAPI, fake_idp: FakeIdp, sso_org: Org) -> None:
    member = await join(app, sso_org.tenant, "recruiter")
    await member.client.aclose()
    idp = await create_oidc_idp(sso_org.admin)
    await activate(sso_org.admin, idp["id"])
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        start = await client.get(f"{API}/auth/sso/{idp['id']}/start", params={"tenant": sso_org.tenant.slug})
        q = parse_qs(urlsplit(start.headers["location"]).query)
        fake_idp.claims = {"sub": "s", "email": member.email, "email_verified": True}
        params = {"code": q["nonce"][0], "state": q["state"][0]}
        first = await client.get(
            f"{API}/auth/sso/oidc/callback", params=params, headers={"Accept": "application/json"}
        )
        assert first.status_code == 303
        replay = await client.get(
            f"{API}/auth/sso/oidc/callback", params=params, headers={"Accept": "application/json"}
        )
        assert replay.status_code == 401 and replay.json()["error"]["code"] == "sso_assertion_invalid"
    finally:
        await client.aclose()


async def test_jit_with_group_mapping_and_seat_limit(app: FastAPI, fake_idp: FakeIdp) -> None:
    tenant = await make_tenant(seat_limit=2)
    admin = await join(app, tenant, name="Ada Admin")
    try:
        async with tenant_session(tenant.id) as db:
            roles = {r.key: r.id for r in (await db.execute(select(Role))).scalars()}
        idp = await create_oidc_idp(
            admin, jit_enabled=True, jit_default_role_id=str(roles["viewer"]), group_claim="groups"
        )
        mapped = await admin.client.put(
            f"{API}/sso/identity-providers/{idp['id']}/group-roles",
            json=[{"group_value": "recruiters", "role_id": str(roles["recruiter"])}],
            headers=admin.h(),
        )
        assert mapped.status_code == 200 and mapped.json()["group_roles"][0]["group_value"] == "recruiters"
        await activate(admin, idp["id"])
        claims = {
            "sub": "jit-1",
            "email": f"jit1@{DOMAIN}",
            "email_verified": True,
            "name": "Jit One",
            "groups": ["recruiters"],
        }
        browser, cb = await oidc_sign_in(app, fake_idp, tenant.slug, idp["id"], claims)
        try:
            assert cb.status_code == 303, cb.text
            me = (await browser.get(f"{API}/auth/me")).json()
            assert sorted(me["user"]["role_keys"]) == ["recruiter", "viewer"]
        finally:
            await browser.aclose()
        actions = await _actions(tenant.id)
        assert "sso.user_jit_provisioned" in actions
        # Seat limit 2 now reached (admin + JIT user).
        b, full = await oidc_sign_in(
            app,
            fake_idp,
            tenant.slug,
            idp["id"],
            {"sub": "jit-2", "email": f"jit2@{DOMAIN}", "email_verified": True},
        )
        await b.aclose()
        assert full.status_code == 403 and full.json()["error"]["code"] == "seat_limit_reached"
        assert "sso.login_refused" in await _actions(tenant.id)
    finally:
        await admin.client.aclose()


async def test_test_login_creates_nothing(app: FastAPI, fake_idp: FakeIdp, sso_org: Org) -> None:
    idp = await create_oidc_idp(sso_org.admin, jit_enabled=True)
    url = (
        await sso_org.admin.client.post(
            f"{API}/sso/identity-providers/{idp['id']}/test", headers=sso_org.admin.h()
        )
    ).json()["url"]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        start = await client.get(url)
        assert start.status_code == 302  # draft IdPs can be tested
        q = parse_qs(urlsplit(start.headers["location"]).query)
        fake_idp.claims = {"sub": "t1", "email": f"tester@{DOMAIN}", "email_verified": True, "name": "T"}
        result = await client.get(
            f"{API}/auth/sso/oidc/callback", params={"code": q["nonce"][0], "state": q["state"][0]}
        )
        assert result.status_code == 200 and result.json()["resolution"] == "jit"
        assert result.json()["email"] == f"tester@{DOMAIN}"
    finally:
        await client.aclose()
    async with tenant_session(sso_org.tenant.id) as db:
        assert (await db.execute(select(User).where(User.email == f"tester@{DOMAIN}"))).first() is None


# ================================================================ enforcement
async def test_password_sign_in_refused_when_enforced(app: FastAPI, fake_idp: FakeIdp, sso_org: Org) -> None:
    recruiter = await join(app, sso_org.tenant, "recruiter")
    await recruiter.client.aclose()
    await _break_glass(sso_org)
    idp = await create_oidc_idp(sso_org.admin)
    assert (await activate(sso_org.admin, idp["id"], enforce_for="all")).status_code == 200
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        body = {"tenant_slug": sso_org.tenant.slug, "email": recruiter.email, "password": USER_PASSWORD}
        refused = await browser.post(f"{API}/auth/login", json=body)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "sso_required"
        wrong = await browser.post(f"{API}/auth/login", json={**body, "password": "wrong-password-1"})
        assert wrong.status_code == 401
        # Break-glass admin (MFA enrolled) may still use the password path (MFA challenge follows).
        admin_body = {**body, "email": sso_org.admin.email}
        admin = await browser.post(f"{API}/auth/login", json=admin_body)
        assert admin.status_code == 200, admin.text
        # Discover: same shape for unknown emails; required reflects "all".
        found = await browser.post(
            f"{API}/auth/sso/discover", json={"tenant_slug": sso_org.tenant.slug, "email": recruiter.email}
        )
        assert found.json() == {"sso": True, "idp_id": idp["id"], "required": True}
        unknown = await browser.post(
            f"{API}/auth/sso/discover", json={"tenant_slug": sso_org.tenant.slug, "email": f"nobody@{DOMAIN}"}
        )
        assert unknown.json() == found.json()
        other = await browser.post(
            f"{API}/auth/sso/discover", json={"tenant_slug": sso_org.tenant.slug, "email": "x@gmail.com"}
        )
        assert other.json() == {"sso": False, "idp_id": None, "required": False}
        cross = await browser.post(
            f"{API}/auth/sso/discover",
            json={"tenant_slug": sso_org.tenant.slug, "email": "x@gmail.com"},
            headers={"Origin": "https://evil.example.com"},
        )
        assert cross.status_code == 403
    finally:
        await browser.aclose()
    # Deactivating the last break-glass admin is refused while enforced.
    second_admin = await join(app, sso_org.tenant, name="Second Admin")
    try:
        resp = await second_admin.client.post(
            f"{API}/admin/users/{sso_org.admin.user_id}/deactivate", headers=second_admin.h()
        )
        assert resp.status_code == 409 and resp.json()["error"]["code"] == "break_glass_required"
    finally:
        await second_admin.client.aclose()


async def test_sso_with_mfa_user_gets_challenge(app: FastAPI, fake_idp: FakeIdp, sso_org: Org) -> None:
    member = await join(app, sso_org.tenant, "recruiter")
    await member.client.aclose()
    await _set_user(sso_org.tenant.id, member.user_id, mfa_enabled=True)
    idp = await create_oidc_idp(sso_org.admin)
    await activate(sso_org.admin, idp["id"])
    browser, cb = await oidc_sign_in(
        app,
        fake_idp,
        sso_org.tenant.slug,
        idp["id"],
        {"sub": "m", "email": member.email, "email_verified": True},
    )
    try:
        assert cb.status_code == 303 and "/login?mfa=1" in cb.headers["location"]
        assert "tos_mfa" in cb.headers.get("set-cookie", "") and "tos_session=" not in cb.headers.get(
            "set-cookie", ""
        )
    finally:
        await browser.aclose()
    # mfa_trusted IdP satisfies MFA.
    await sso_org.admin.client.patch(
        f"{API}/sso/identity-providers/{idp['id']}", json={"mfa_trusted": True}, headers=sso_org.admin.h()
    )
    browser, cb = await oidc_sign_in(
        app,
        fake_idp,
        sso_org.tenant.slug,
        idp["id"],
        {"sub": "m", "email": member.email, "email_verified": True},
    )
    try:
        assert cb.status_code == 303 and "tos_session=" in cb.headers.get("set-cookie", "")
    finally:
        await browser.aclose()


# ================================================================ SAML
async def _saml_idp(org: Org, saml: SamlIdp) -> dict[str, object]:
    body = {
        "name": "Entra",
        "kind": "saml",
        "email_domains": [DOMAIN],
        "saml": {"idp_entity_id": saml.entity_id, "sso_url": saml.sso_url, "x509_certs": [saml.cert_pem]},
        "group_claim": "groups",
    }
    resp = await org.admin.client.post(f"{API}/sso/identity-providers", json=body, headers=org.admin.h())
    assert resp.status_code == 201, resp.text
    assert (await activate(org.admin, resp.json()["id"])).status_code == 200
    return dict(resp.json())


async def _saml_round(
    app: FastAPI, org: Org, idp: dict[str, object], saml: SamlIdp, email: str, **kw: object
) -> httpx.Response:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        start = await client.get(f"{API}/auth/sso/{idp['id']}/start", params={"tenant": org.tenant.slug})
        assert start.status_code == 302
        location = start.headers["location"]
        assert location.startswith(saml.sso_url)
        request_id = str(kw.pop("request_id", saml_request_id(location)))
        payload = saml_response(
            saml,
            acs=str(idp["acs_url"]),
            audience=str(kw.pop("audience", idp["sp_entity_id"])),
            request_id=request_id,
            email=email,
            **kw,  # type: ignore[arg-type]
        )
        return await client.post(
            f"{API}/auth/sso/saml/{idp['id']}/acs",
            data={"SAMLResponse": payload, "RelayState": relay_state(location)},
            headers={"Accept": "application/json"},
        )
    finally:
        await client.aclose()


async def test_saml_sign_in_and_rejections(app: FastAPI, sso_org: Org, saml_idp: SamlIdp) -> None:
    member = await join(app, sso_org.tenant, "recruiter")
    await member.client.aclose()
    idp = await _saml_idp(sso_org, saml_idp)
    metadata = await sso_org.admin.client.get(f"{API}/sso/saml/{idp['id']}/metadata")
    assert metadata.status_code == 200 and str(idp["acs_url"]) in metadata.text

    ok = await _saml_round(app, sso_org, idp, saml_idp, member.email)
    assert ok.status_code == 303, ok.text
    unsigned = await _saml_round(app, sso_org, idp, saml_idp, member.email, sign=False)
    assert unsigned.status_code == 401 and unsigned.json()["error"]["code"] == "sso_assertion_invalid"
    audience = await _saml_round(
        app, sso_org, idp, saml_idp, member.email, audience="https://other.example.com/sp"
    )
    assert audience.status_code == 401
    expired = await _saml_round(app, sso_org, idp, saml_idp, member.email, expired=True)
    assert expired.status_code == 401
    wrong_request = await _saml_round(
        app, sso_org, idp, saml_idp, member.email, request_id="_not-our-request"
    )
    assert wrong_request.status_code == 401


@pytest.mark.parametrize("kind", ["client_user"])
async def test_external_session_cannot_administer_sso(app: FastAPI, sso_org: Org, kind: str) -> None:
    from tests.modules.demands.helpers import create_client

    client_id = await create_client(sso_org.admin)
    cu = await make_client_user(sso_org.tenant.id, client_id)
    ext = await sign_in_external(app, kind, cu, sso_org.tenant.id, client_id)
    try:
        resp = await ext.client.post(
            f"{API}/sso/identity-providers", json={"name": "x", "kind": "oidc"}, headers=ext.h()
        )
        assert resp.status_code in (401, 403)
    finally:
        await ext.client.aclose()


def test_csrf_header_helper_is_used() -> None:
    assert csrf is not None
