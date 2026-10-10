"""Epic 50 (FR-250..FR-252, AD-48): Client Portal SSO -- per-Client IdP administration, client sign-in (link,
JIT, MFA, suspension), discovery, test login, SAML, enforcement + break-glass and approval-link opt-out."""

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.core import secure_links
from app.core.database import tenant_session
from app.core.mailer import EmailMessage, OutboxMailer, get_mailer
from app.models.external_auth import ClientUser
from app.models.sso import ClientUserIdentity, IdentityProvider
from app.services import mfa_service
from app.services.privacy.registry import ErasureContext
from app.services.sso_erasure import ClientSsoIdentitiesHandler
from tests.modules.demands.helpers import create_client
from tests.modules.identity.conftest import USER_PASSWORD, Org, join
from tests.modules.sso.client_helpers import (
    audits,
    client_oidc_sign_in,
    client_saml_sign_in,
    client_user,
    client_user_count,
    create_client_oidc,
    email_at,
    identity_count,
)
from tests.modules.sso.conftest import API, DOMAIN, FakeIdp, SamlIdp, activate, create_oidc_idp

LINK = re.compile(r"#t=([A-Za-z0-9_-]+)")
CODE = re.compile(r"code on the sign-in page: (\d{6})")


def _mails(tenant_id: uuid.UUID, kind: str) -> list[EmailMessage]:
    mailer = get_mailer()
    assert isinstance(mailer, OutboxMailer)
    return [m for m in mailer.sent if m.tenant_id == tenant_id and m.kind == kind]


def _totp(secret: str) -> str:
    return mfa_service.totp_code(secret, mfa_service.current_step(datetime.now(UTC)))


def _idps(client_id: uuid.UUID) -> str:
    return f"{API}/clients/{client_id}/sso/identity-providers"


# ================================================================ administration (Story 50.1)
async def test_client_idp_admin_validation_and_internal_invisibility(fake_idp: FakeIdp, sso_org: Org) -> None:
    admin = sso_org.admin
    client_a = await create_client(admin)
    client_b = await create_client(admin)
    base = {
        "name": "Acme Okta",
        "kind": "oidc",
        "oidc": {"issuer": "https://idp.example.test", "client_id": "x"},
    }
    empty = await admin.client.post(_idps(client_a), json={**base, "email_domains": []}, headers=admin.h())
    assert empty.status_code == 422 and empty.json()["error"]["code"] == "allowed_domains_required"
    bad_perm = await admin.client.post(
        _idps(client_a),
        json={**base, "email_domains": [DOMAIN], "jit_default_permissions": ["sso:manage"]},
        headers=admin.h(),
    )
    assert bad_perm.status_code == 422 and bad_perm.json()["error"]["code"] == "invalid_portal_permission"
    unknown_client = await admin.client.post(
        _idps(uuid.uuid4()), json={**base, "email_domains": [DOMAIN]}, headers=admin.h()
    )
    assert unknown_client.status_code == 404

    idp = await create_client_oidc(admin, client_a, jit_default_permissions=["time:approve"])
    assert idp["client_id"] == str(client_a) and idp["status"] == "active" and idp["has_secret"]
    assert "s3cret" not in str(idp) and idp["redirect_uri"].endswith("/api/v1/auth/sso/oidc/callback")
    # Another Client's active IdP cannot take the same domain.
    other = await create_client_oidc(admin, client_b, activate=False)
    clash = await admin.client.patch(
        f"{_idps(client_b)}/{other['id']}", json={"status": "active"}, headers=admin.h()
    )
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "sso_domain_taken"
    # Client B cannot read Client A's IdP through its own path.
    assert (await admin.client.get(f"{_idps(client_b)}/{idp['id']}")).status_code == 404
    # Internal SSO admin and internal discovery never see client IdPs.
    internal = await admin.client.get(f"{API}/sso/identity-providers")
    assert idp["id"] not in [i["id"] for i in internal.json()]
    assert (await admin.client.get(f"{API}/sso/identity-providers/{idp['id']}")).status_code == 404
    found = await admin.client.post(
        f"{API}/auth/sso/discover", json={"tenant_slug": sso_org.tenant.slug, "email": email_at()}
    )
    assert found.json() == {"sso": False, "idp_id": None, "required": False}
    # An internal IdP may serve the same domain (separate discovery).
    internal_idp = await create_oidc_idp(admin)
    assert (await activate(admin, internal_idp["id"])).status_code == 200
    test = await admin.client.post(f"{_idps(client_a)}/{idp['id']}/test", headers=admin.h())
    assert test.json()["url"].startswith(f"/api/v1/client-portal/auth/sso/{idp['id']}/start?tenant=")
    assert "client_sso.idp_created" in await _actions(sso_org)
    # Delete an unlinked draft.
    gone = await admin.client.delete(f"{_idps(client_b)}/{other['id']}", headers=admin.h())
    assert gone.status_code == 204


async def _actions(org: Org) -> list[str]:
    from app.core.audit import AuditLog

    async with tenant_session(org.tenant.id) as db:
        return list((await db.execute(select(AuditLog.action))).scalars())


async def test_db_check_refuses_client_audience_without_client(sso_org: Org) -> None:
    with pytest.raises(IntegrityError):
        async with tenant_session(sso_org.tenant.id) as db:
            await db.execute(
                text(
                    "INSERT INTO identity_providers (id, tenant_id, name, kind, audience) "
                    "VALUES (:id, :t, 'raw', 'oidc', 'client')"
                ),
                {"id": uuid.uuid4(), "t": sso_org.tenant.id},
            )


async def test_permission_and_account_manager_default(app: FastAPI, sso_org: Org) -> None:
    client_a = await create_client(sso_org.admin)
    recruiter = await join(app, sso_org.tenant, "recruiter")
    manager = await join(app, sso_org.tenant, "account_manager")
    try:
        denied = await recruiter.client.get(_idps(client_a))
        assert denied.status_code == 403
        assert denied.json()["error"]["details"]["required_permission"] == "client_sso:manage"
        assert (await manager.client.get(_idps(client_a))).status_code == 200
    finally:
        await recruiter.client.aclose()
        await manager.client.aclose()


# ================================================================ sign-in (Story 50.2)
async def test_link_by_verified_email_then_subject_and_session(
    app: FastAPI, fake_idp: FakeIdp, sso_org: Org
) -> None:
    client_a = await create_client(sso_org.admin)
    email = email_at()
    cu_id = await client_user(sso_org.tenant.id, client_a, email, status="invited")
    idp = await create_client_oidc(sso_org.admin, client_a)
    claims = {"sub": "okta|cora", "email": email, "email_verified": True}
    browser, cb = await client_oidc_sign_in(app, fake_idp, sso_org.tenant.slug, idp["id"], claims)
    try:
        assert cb.status_code == 303, cb.text
        assert cb.headers["location"].endswith("/client/approvals")
        cookies = cb.headers.get("set-cookie", "")
        assert "tos_client=" in cookies and "tos_session=" not in cookies
        me = await browser.get(f"{API}/client-portal/me")
        assert me.status_code == 200, me.text
        assert me.json()["id"] == str(cu_id) and me.json()["org_id"] == str(client_a)
        assert (await browser.get(f"{API}/auth/me")).status_code == 401
    finally:
        await browser.aclose()
    async with tenant_session(sso_org.tenant.id) as db:
        link = (await db.execute(select(ClientUserIdentity))).scalar_one()
        cu = await db.get(ClientUser, cu_id)
    assert link.client_user_id == cu_id and link.subject == "okta|cora"
    assert cu is not None and cu.status == "active"
    assert await audits(sso_org.tenant.id, "client_sso.identity_linked")
    # Second sign-in resolves by subject even when the email claim changed.
    browser, cb = await client_oidc_sign_in(
        app,
        fake_idp,
        sso_org.tenant.slug,
        idp["id"],
        {"sub": "okta|cora", "email": "other@x.example", "email_verified": False},
    )
    await browser.aclose()
    assert cb.status_code == 303
    listed = await sso_org.admin.client.get(_idps(client_a))
    assert listed.json()[0]["linked_users"] == 1 and listed.json()[0]["last_login_at"]


@pytest.mark.parametrize(
    ("claims", "code"),
    [
        ({"email_verified": False}, "sso_email_unverified"),
        ({"email": "cora@elsewhere.example.org"}, "sso_domain_not_allowed"),
        ({"email": None}, "sso_no_client_user"),  # replaced below by an unknown in-domain email
    ],
)
async def test_refusals(
    app: FastAPI, fake_idp: FakeIdp, sso_org: Org, claims: dict[str, object], code: str
) -> None:
    client_a = await create_client(sso_org.admin)
    email = email_at()
    await client_user(sso_org.tenant.id, client_a, email)
    await client_user(sso_org.tenant.id, client_a, "cora@elsewhere.example.org")
    idp = await create_client_oidc(sso_org.admin, client_a)
    full = {"sub": f"s-{uuid.uuid4().hex[:6]}", "email": email, "email_verified": True, **claims}
    if full["email"] is None:
        full["email"] = email_at()
    browser, cb = await client_oidc_sign_in(app, fake_idp, sso_org.tenant.slug, idp["id"], full)
    try:
        assert cb.status_code == 401 and cb.json()["error"]["code"] == code
        assert (await browser.get(f"{API}/client-portal/me")).status_code == 401
    finally:
        await browser.aclose()
    assert await identity_count(sso_org.tenant.id) == 0
    assert await audits(sso_org.tenant.id, "client_sso.login_refused")


async def test_browser_failure_redirects_to_client_sign_in(
    app: FastAPI, fake_idp: FakeIdp, sso_org: Org
) -> None:
    client_a = await create_client(sso_org.admin)
    idp = await create_client_oidc(sso_org.admin, client_a)
    browser, start = None, None
    from urllib.parse import parse_qs, urlsplit

    import httpx

    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        start = await browser.get(
            f"{API}/client-portal/auth/sso/{idp['id']}/start", params={"tenant": sso_org.tenant.slug}
        )
        query = parse_qs(urlsplit(start.headers["location"]).query)
        fake_idp.claims = {"sub": "nobody", "email": email_at(), "email_verified": True}
        cb = await browser.get(
            f"{API}/auth/sso/oidc/callback", params={"code": query["nonce"][0], "state": query["state"][0]}
        )
        assert cb.status_code == 303
        assert "/client/sign-in?sso_error=sso_no_client_user" in cb.headers["location"]
        # The internal start never serves a client IdP.
        internal = await browser.get(
            f"{API}/auth/sso/{idp['id']}/start", params={"tenant": sso_org.tenant.slug}
        )
        assert internal.status_code == 303 and "/login?sso_error=" in internal.headers["location"]
    finally:
        await browser.aclose()


async def test_jit_off_and_on(app: FastAPI, fake_idp: FakeIdp, sso_org: Org) -> None:
    client_a = await create_client(sso_org.admin)
    idp = await create_client_oidc(
        sso_org.admin, client_a, jit_default_permissions=["time:approve", "invoices:view"]
    )
    email = email_at()
    claims = {"sub": "jit-1", "email": email, "email_verified": True, "name": "Jit Person"}
    browser, cb = await client_oidc_sign_in(app, fake_idp, sso_org.tenant.slug, idp["id"], claims)
    await browser.aclose()
    assert cb.status_code == 401 and cb.json()["error"]["code"] == "sso_no_client_user"
    assert await client_user_count(sso_org.tenant.id) == 0
    on = await sso_org.admin.client.patch(
        f"{_idps(client_a)}/{idp['id']}", json={"jit_enabled": True}, headers=sso_org.admin.h()
    )
    assert on.status_code == 200
    browser, cb = await client_oidc_sign_in(app, fake_idp, sso_org.tenant.slug, idp["id"], claims)
    try:
        assert cb.status_code == 303, cb.text
        me = (await browser.get(f"{API}/client-portal/me")).json()
        assert me["org_id"] == str(client_a)
        assert me["portal_permissions"] == ["invoices:view", "time:approve"]
    finally:
        await browser.aclose()
    async with tenant_session(sso_org.tenant.id) as db:
        cu = (await db.execute(select(ClientUser))).scalar_one()
    assert (
        cu.status == "active"
        and not cu.restricted
        and cu.invited_by is None
        and cu.display_name == "Jit Person"
    )
    assert await audits(sso_org.tenant.id, "client_sso.user_jit_provisioned")


async def test_mfa_enforced_requires_totp_unless_idp_trusted(
    app: FastAPI, fake_idp: FakeIdp, sso_org: Org
) -> None:
    client_a = await create_client(sso_org.admin)
    email = email_at()
    await client_user(sso_org.tenant.id, client_a, email, mfa_enforced=True)
    idp = await create_client_oidc(sso_org.admin, client_a)
    claims = {"sub": "mfa-1", "email": email, "email_verified": True}
    browser, cb = await client_oidc_sign_in(app, fake_idp, sso_org.tenant.slug, idp["id"], claims)
    try:
        assert cb.status_code == 303 and "/client/sign-in?mfa=enroll" in cb.headers["location"]
        assert "tos_client=" not in cb.headers.get("set-cookie", "") and "tos_mfa=" in cb.headers.get(
            "set-cookie", ""
        )
        enrol = await browser.post(f"{API}/client-portal/auth/sso/mfa-enrollment")
        assert enrol.status_code == 200 and enrol.json()["status"] == "mfa_enrollment_required"
        secret = enrol.json()["enrollment"]["secret"]
        done = await browser.post(f"{API}/client-portal/auth/mfa/verify", json={"code": _totp(secret)})
        assert done.status_code == 200 and done.json()["status"] == "signed_in", done.text
        assert (await browser.get(f"{API}/client-portal/me")).status_code == 200
    finally:
        await browser.aclose()
    # Enrolled now: the next SSO sign-in asks for the code (mfa=1); an mfa_trusted IdP skips it.
    browser, cb = await client_oidc_sign_in(app, fake_idp, sso_org.tenant.slug, idp["id"], claims)
    await browser.aclose()
    assert "/client/sign-in?mfa=1" in cb.headers["location"]
    await sso_org.admin.client.patch(
        f"{_idps(client_a)}/{idp['id']}", json={"mfa_trusted": True}, headers=sso_org.admin.h()
    )
    browser, cb = await client_oidc_sign_in(app, fake_idp, sso_org.tenant.slug, idp["id"], claims)
    await browser.aclose()
    assert cb.status_code == 303 and "tos_client=" in cb.headers.get("set-cookie", "")


async def test_suspension_ends_sso_session_and_refuses_sign_in(
    app: FastAPI, fake_idp: FakeIdp, sso_org: Org
) -> None:
    client_a = await create_client(sso_org.admin)
    email = email_at()
    cu_id = await client_user(sso_org.tenant.id, client_a, email)
    idp = await create_client_oidc(sso_org.admin, client_a)
    claims = {"sub": "susp-1", "email": email, "email_verified": True}
    browser, cb = await client_oidc_sign_in(app, fake_idp, sso_org.tenant.slug, idp["id"], claims)
    try:
        assert cb.status_code == 303
        assert (await browser.get(f"{API}/client-portal/me")).status_code == 200
        resp = await sso_org.admin.client.post(
            f"{API}/clients/{client_a}/portal-users/{cu_id}/suspend", headers=sso_org.admin.h()
        )
        assert resp.status_code == 200, resp.text
        assert (await browser.get(f"{API}/client-portal/me")).status_code == 401
    finally:
        await browser.aclose()
    browser, cb = await client_oidc_sign_in(app, fake_idp, sso_org.tenant.slug, idp["id"], claims)
    await browser.aclose()
    assert cb.status_code == 403 and cb.json()["error"]["code"] == "client_user_inactive"


async def test_test_login_reports_resolution_and_creates_nothing(
    app: FastAPI, fake_idp: FakeIdp, sso_org: Org
) -> None:
    client_a = await create_client(sso_org.admin)
    email = email_at()
    await client_user(sso_org.tenant.id, client_a, email)
    idp = await create_client_oidc(sso_org.admin, client_a, activate=False, jit_enabled=True)
    browser, cb = await client_oidc_sign_in(
        app,
        fake_idp,
        sso_org.tenant.slug,
        idp["id"],
        {"sub": "t1", "email": email, "email_verified": True},
        test=True,
    )
    await browser.aclose()
    assert (
        cb.status_code == 200
        and cb.json()["resolution"] == "email_match"
        and cb.json()["audience"] == "client"
    )
    browser, cb = await client_oidc_sign_in(
        app,
        fake_idp,
        sso_org.tenant.slug,
        idp["id"],
        {"sub": "t2", "email": email_at(), "email_verified": True},
        test=True,
    )
    await browser.aclose()
    assert cb.json()["resolution"] == "jit"
    browser, cb = await client_oidc_sign_in(
        app,
        fake_idp,
        sso_org.tenant.slug,
        idp["id"],
        {"sub": "t3", "email": "a@b.example.org", "email_verified": True},
        test=True,
    )
    await browser.aclose()
    assert cb.json()["resolution"] == "refused" and cb.json()["reason"] == "domain_not_allowed"
    assert await identity_count(sso_org.tenant.id) == 0 and await client_user_count(sso_org.tenant.id) == 1


async def test_client_discovery_uniform(fake_idp: FakeIdp, sso_org: Org) -> None:
    client_a = await create_client(sso_org.admin)
    idp = await create_client_oidc(sso_org.admin, client_a)
    c = sso_org.admin.client
    known = await c.post(
        f"{API}/client-portal/auth/sso/discover",
        json={"tenant_slug": sso_org.tenant.slug, "email": email_at()},
    )
    assert known.json() == {"sso": True, "idp_id": idp["id"], "required": False}
    none = {"sso": False, "idp_id": None, "required": False}
    unknown_domain = await c.post(
        f"{API}/client-portal/auth/sso/discover",
        json={"tenant_slug": sso_org.tenant.slug, "email": "x@gmail.com"},
    )
    unknown_tenant = await c.post(
        f"{API}/client-portal/auth/sso/discover",
        json={"tenant_slug": "no-such-tenant-x", "email": email_at()},
    )
    assert unknown_domain.json() == none and unknown_tenant.json() == none
    cross = await c.post(
        f"{API}/client-portal/auth/sso/discover",
        json={"tenant_slug": sso_org.tenant.slug, "email": email_at()},
        headers={"Origin": "https://evil.example.com"},
    )
    assert cross.status_code == 403


async def test_saml_client_idp_metadata_import_and_sign_in(
    app: FastAPI, sso_org: Org, saml_idp: SamlIdp
) -> None:
    client_a = await create_client(sso_org.admin)
    email = email_at()
    cu_id = await client_user(sso_org.tenant.id, client_a, email)
    admin = sso_org.admin
    created = await admin.client.post(
        _idps(client_a),
        json={"name": "Entra A", "kind": "saml", "email_domains": [DOMAIN]},
        headers=admin.h(),
    )
    assert created.status_code == 201, created.text
    idp_id = created.json()["id"]
    incomplete = await admin.client.patch(
        f"{_idps(client_a)}/{idp_id}", json={"status": "active"}, headers=admin.h()
    )
    assert incomplete.status_code == 422 and incomplete.json()["error"]["code"] == "sso_config_incomplete"
    body = "".join(saml_idp.cert_pem.strip().splitlines()[1:-1])
    metadata = (
        '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
        f'entityID="{saml_idp.entity_id}"><md:IDPSSODescriptor '
        'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
        '<md:KeyDescriptor use="signing"><ds:KeyInfo xmlns:ds="http://www.w3.org/2000/09/xmldsig#"><ds:X509Data>'
        f"<ds:X509Certificate>{body}</ds:X509Certificate></ds:X509Data></ds:KeyInfo></md:KeyDescriptor>"
        '<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
        f'Location="{saml_idp.sso_url}"/></md:IDPSSODescriptor></md:EntityDescriptor>'
    )
    imported = await admin.client.post(
        f"{_idps(client_a)}/{idp_id}/metadata-import", json={"metadata_xml": metadata}, headers=admin.h()
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["config"]["idp_entity_id"] == saml_idp.entity_id
    bad = await admin.client.post(
        f"{_idps(client_a)}/{idp_id}/metadata-import",
        json={"metadata_xml": "<not-metadata/>" * 3},
        headers=admin.h(),
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "sso_metadata_invalid"
    sp = await admin.client.get(f"{_idps(client_a)}/{idp_id}/sp-metadata")
    assert sp.status_code == 200 and imported.json()["acs_url"] in sp.text
    active = await admin.client.patch(
        f"{_idps(client_a)}/{idp_id}", json={"status": "active"}, headers=admin.h()
    )
    assert active.status_code == 200, active.text
    browser, resp = await client_saml_sign_in(app, sso_org.tenant.slug, active.json(), saml_idp, email)
    try:
        assert resp.status_code == 303, resp.text
        assert (await browser.get(f"{API}/client-portal/me")).json()["id"] == str(cu_id)
    finally:
        await browser.aclose()
    # An http SSO URL is refused.
    http = await admin.client.patch(
        f"{_idps(client_a)}/{idp_id}",
        json={
            "saml": {
                "idp_entity_id": "e",
                "sso_url": "http://idp.example/sso",
                "x509_certs": [saml_idp.cert_pem],
            }
        },
        headers=admin.h(),
    )
    assert http.status_code == 422 and http.json()["error"]["code"] == "sso_https_required"


# ================================================================ enforcement + break-glass (Story 50.3)
async def test_enforcement_magic_link_and_break_glass(app: FastAPI, fake_idp: FakeIdp, sso_org: Org) -> None:
    admin = sso_org.admin
    client_a = await create_client(admin)
    tenant = sso_org.tenant
    plain_email, glass_email = email_at(), email_at()
    plain = await client_user(tenant.id, client_a, plain_email)
    glass = await client_user(tenant.id, client_a, glass_email)
    settings_url = f"{API}/clients/{client_a}/sso/settings"
    refused = await admin.client.put(settings_url, json={"sso_required": True}, headers=admin.h())
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "client_idp_required"
    portal = app_client(app)
    try:
        # A link issued before enforcement is refused afterwards.
        await portal.post(
            f"{API}/client-portal/auth/request-link", json={"tenant_slug": tenant.slug, "email": plain_email}
        )
        old = _mails(tenant.id, "client_sign_in")[-1]
        old_token, old_code = LINK.search(old.text).group(1), CODE.search(old.text).group(1)  # type: ignore[union-attr]
        await create_client_oidc(admin, client_a)
        on = await admin.client.put(settings_url, json={"sso_required": True}, headers=admin.h())
        assert (
            on.status_code == 200
            and on.json()["sso_required"]
            and on.json()["warnings"] == ["no_break_glass"]
        )
        verify = await portal.post(f"{API}/client-portal/auth/verify-link", json={"token": old_token})
        assert verify.status_code == 403 and verify.json()["error"]["code"] == "sso_required"
        assert verify.json()["error"]["details"]["sign_in_url"].endswith(
            f"/client/sign-in?tenant={tenant.slug}"
        )
        code = await portal.post(
            f"{API}/client-portal/auth/verify-code",
            json={"tenant_slug": tenant.slug, "email": plain_email, "code": old_code},
        )
        assert code.status_code == 403 and code.json()["error"]["code"] == "sso_required"
        # New requests: same response, guidance email instead of a link.
        before = len(_mails(tenant.id, "client_sign_in"))
        resp = await portal.post(
            f"{API}/client-portal/auth/request-link", json={"tenant_slug": tenant.slug, "email": plain_email}
        )
        assert resp.status_code == 202 and resp.json() == {"status": "check_email"}
        assert len(_mails(tenant.id, "client_sign_in")) == before
        guidance = _mails(tenant.id, "client_sso_required")[-1]
        assert (
            guidance.to == plain_email
            and "/client/sign-in?tenant=" in guidance.text
            and not CODE.search(guidance.text)
        )
        # Discovery reports "required".
        disc = await portal.post(
            f"{API}/client-portal/auth/sso/discover", json={"tenant_slug": tenant.slug, "email": plain_email}
        )
        assert disc.json()["required"] is True
        # Break-glass: exemption forces TOTP; magic link + code + TOTP still works.
        other_client = await create_client(admin)
        foreign = await admin.client.post(
            f"{API}/clients/{other_client}/sso/exemptions",
            json={"client_user_id": str(glass), "reason": "Ops lead"},
            headers=admin.h(),
        )
        assert foreign.status_code == 404
        added = await admin.client.post(
            f"{API}/clients/{client_a}/sso/exemptions",
            json={"client_user_id": str(glass), "reason": "Ops lead"},
            headers=admin.h(),
        )
        assert added.status_code == 201, added.text
        assert added.json()["mfa_enforced"] is True
        assert (await admin.client.get(settings_url)).json()["warnings"] == []
        await portal.post(
            f"{API}/client-portal/auth/request-link", json={"tenant_slug": tenant.slug, "email": glass_email}
        )
        mail = _mails(tenant.id, "client_sign_in")[-1]
        assert mail.to == glass_email
        signed = await portal.post(
            f"{API}/client-portal/auth/verify-code",
            json={"tenant_slug": tenant.slug, "email": glass_email, "code": CODE.search(mail.text).group(1)},  # type: ignore[union-attr]
        )
        assert signed.status_code == 200 and signed.json()["status"] == "mfa_enrollment_required"
        removed = await admin.client.delete(
            f"{API}/clients/{client_a}/sso/exemptions/{glass}", headers=admin.h()
        )
        assert removed.status_code == 204
        assert "client_sso.enforcement_changed" in await _actions(sso_org)
    finally:
        await portal.aclose()
    # Internal Users' password sign-in is unaffected by client enforcement.
    member = await join(app, tenant, "recruiter")
    try:
        await member.client.post(f"{API}/auth/logout", headers=member.h())
        login = await member.client.post(
            f"{API}/auth/login",
            json={"tenant_slug": tenant.slug, "email": member.email, "password": USER_PASSWORD},
        )
        assert login.status_code == 200, login.text
    finally:
        await member.client.aclose()
    del plain


def app_client(app: FastAPI) -> "httpx.AsyncClient":  # noqa: F821
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_approval_links_opt_out(sso_org: Org) -> None:
    admin = sso_org.admin
    client_a = await create_client(admin)
    cu = await client_user(sso_org.tenant.id, client_a, email_at())
    async with tenant_session(sso_org.tenant.id) as db:
        _, token = await secure_links.issue(
            db,
            tenant_id=sso_org.tenant.id,
            purpose="client_approval",
            subject_type="approval_request",
            subject_id=uuid.uuid4(),
            ttl=timedelta(days=1),
            metadata={"addressee_kind": "client_user", "addressee_id": str(cu), "position": 1},
        )
    allowed = await admin.client.get(f"{API}/public/links/{token}/client-approval")
    assert allowed.status_code != 403  # P2 behaviour (the fake request is simply not found)
    off = await admin.client.put(
        f"{API}/clients/{client_a}/sso/settings", json={"approval_links_allowed": False}, headers=admin.h()
    )
    assert off.status_code == 200 and off.json()["approval_links_allowed"] is False
    blocked = await admin.client.get(f"{API}/public/links/{token}/client-approval")
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "sso_required"
    decide = await admin.client.post(
        f"{API}/public/links/{token}/client-approval/decide", json={"decision": "approve"}, headers=admin.h()
    )
    assert decide.status_code == 403 and decide.json()["error"]["code"] == "sso_required"


async def test_erasure_handler_removes_links_and_exemptions(fake_idp: FakeIdp, sso_org: Org) -> None:
    client_a = await create_client(sso_org.admin)
    cu = await client_user(sso_org.tenant.id, client_a, email_at())
    idp = await create_client_oidc(sso_org.admin, client_a)
    added = await sso_org.admin.client.post(
        f"{API}/clients/{client_a}/sso/exemptions",
        json={"client_user_id": str(cu), "reason": "Break glass"},
        headers=sso_org.admin.h(),
    )
    assert added.status_code == 201
    async with tenant_session(sso_org.tenant.id) as db:
        db.add(
            ClientUserIdentity(
                id=uuid.uuid4(),
                tenant_id=sso_org.tenant.id,
                client_user_id=cu,
                idp_id=uuid.UUID(idp["id"]),
                subject="x",
            )
        )
    handler = ClientSsoIdentitiesHandler()
    ctx = ErasureContext(
        tenant_id=sso_org.tenant.id,
        candidate_id=cu,
        run_id=uuid.uuid4(),
        reason="dsr",
        subject_kind="client_user",
        subject_id=cu,
    )
    async with tenant_session(sso_org.tenant.id) as db:
        assert await handler.verify(db, ctx)
        counts = await handler.erase(db, ctx)
        assert counts == {"client_user_identities": 1, "client_sso_exemptions": 1}
        assert await handler.verify(db, ctx) == []


async def test_internal_admin_cannot_flip_audience(sso_org: Org) -> None:
    """The internal API has no ``audience`` / ``client_id`` fields (extra=forbid)."""
    resp = await sso_org.admin.client.post(
        f"{API}/sso/identity-providers",
        json={"name": "x", "kind": "oidc", "audience": "client", "email_domains": [DOMAIN]},
        headers=sso_org.admin.h(),
    )
    assert resp.status_code == 422
    async with tenant_session(sso_org.tenant.id) as db:
        assert (
            await db.execute(select(IdentityProvider).where(IdentityProvider.audience == "client"))
        ).first() is None
        await db.execute(update(IdentityProvider).values(name=IdentityProvider.name))
