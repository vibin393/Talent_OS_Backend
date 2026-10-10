"""Epic 14 (FR-78, FR-79): Client User access (grant, magic link + code, optional / enforced TOTP, suspend)
and
Vendor User access (invite, password + mandatory TOTP, reset, self-administration with last-admin guard)."""

import re
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update

from app.core.database import tenant_session
from app.core.mailer import EmailMessage, OutboxMailer, get_mailer
from app.core.security import CSRF_COOKIE, CSRF_HEADER
from app.models.clients import Contact
from app.models.vendors import Vendor
from app.services import mfa_service
from tests.modules.demands.helpers import DOrg, other_dorg  # noqa: F401
from tests.modules.identity.conftest import USER_PASSWORD, csrf, new_client
from tests.p2 import make_vendor, make_vendor_user, sign_in_external

API = "/api/v1"
LINK = re.compile(r"#t=([A-Za-z0-9_-]+)")
CODE = re.compile(r"code on the sign-in page: (\d{6})")
STRONG = "Saffron-Harbour-77"


def _mails(tenant_id: uuid.UUID, kind: str) -> list[EmailMessage]:
    mailer = get_mailer()
    assert isinstance(mailer, OutboxMailer)
    return [m for m in mailer.sent if m.tenant_id == tenant_id and m.kind == kind]


def _token(mail: EmailMessage) -> str:
    match = LINK.search(mail.text)
    assert match, mail.text
    return match.group(1)


def _totp(secret: str) -> str:
    return mfa_service.totp_code(secret, mfa_service.current_step(datetime.now(UTC)))


def _h(client: httpx.AsyncClient) -> dict[str, str]:
    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


async def _contact(dorg: DOrg, email: str) -> uuid.UUID:
    async with tenant_session(dorg.tenant_id) as db:
        contact = Contact(
            id=uuid.uuid4(),
            tenant_id=dorg.tenant_id,
            client_id=dorg.client_id,
            name="Cora Client",
            email=email,
        )
        db.add(contact)
        return contact.id


async def _grant(dorg: DOrg, **extra: object) -> dict[str, object]:
    email = f"cora-{uuid.uuid4().hex[:6]}@client.example.com"
    contact_id = await _contact(dorg, email)
    resp = await dorg.admin.client.post(
        f"{API}/clients/{dorg.client_id}/portal-users",
        json={"contact_id": str(contact_id), "portal_permissions": ["submissions:review"], **extra},
        headers=dorg.admin.h(),
    )
    assert resp.status_code == 201, resp.text
    return {**resp.json(), "email": email}


# ================================================================ client users
async def test_client_grant_link_code_and_suspend(app: FastAPI, dorg: DOrg) -> None:
    granted = await _grant(dorg)
    assert granted["status"] == "invited"
    invite = _mails(dorg.tenant_id, "client_portal_invitation")[-1]
    assert invite.to == granted["email"] and CODE.search(invite.text)

    browser = new_client(app)
    try:
        signed = await browser.post(f"{API}/client-portal/auth/verify-link", json={"token": _token(invite)})
        assert signed.status_code == 200, signed.text
        assert (
            signed.json()["status"] == "signed_in" and signed.json()["me"]["principal_type"] == "client_user"
        )
        me = await browser.get(f"{API}/client-portal/me")
        assert me.status_code == 200 and me.json()["org_id"] == str(dorg.client_id)
        replay = await browser.post(f"{API}/client-portal/auth/verify-link", json={"token": _token(invite)})
        assert replay.status_code in (401, 410)
        # CSRF re-issue for the portal session.
        assert (await browser.get(f"{API}/client-portal/csrf")).status_code == 204
        # Internal routes refuse the client session.
        assert (await browser.get(f"{API}/clients/{dorg.client_id}/portal-users")).status_code in (401, 403)
    finally:
        await browser.aclose()

    listed = (await dorg.admin.client.get(f"{API}/clients/{dorg.client_id}/portal-users")).json()
    assert listed[0]["status"] == "active"

    # Code sign-in: request a link, then type the 6-digit code; wrong codes are counted.
    browser = new_client(app)
    try:
        req = await browser.post(
            f"{API}/client-portal/auth/request-link",
            json={"tenant_slug": dorg.tenant.slug, "email": granted["email"]},
        )
        assert req.status_code == 202 and req.json() == {"status": "check_email"}
        unknown = await browser.post(
            f"{API}/client-portal/auth/request-link",
            json={"tenant_slug": dorg.tenant.slug, "email": "nobody@x.example.com"},
        )
        assert unknown.json() == req.json()
        code = CODE.search(_mails(dorg.tenant_id, "client_sign_in")[-1].text).group(1)  # type: ignore[union-attr]
        wrong = "000000" if code != "000000" else "111111"
        body = {"tenant_slug": dorg.tenant.slug, "email": granted["email"]}
        bad = await browser.post(f"{API}/client-portal/auth/verify-code", json={**body, "code": wrong})
        assert bad.status_code == 401 and bad.json()["error"]["code"] == "link_invalid"
        ok = await browser.post(f"{API}/client-portal/auth/verify-code", json={**body, "code": code})
        assert ok.status_code == 200 and ok.json()["status"] == "signed_in"

        # Suspension ends the session at once.
        suspended = await dorg.admin.client.post(
            f"{API}/clients/{dorg.client_id}/portal-users/{granted['id']}/suspend", headers=dorg.admin.h()
        )
        assert suspended.status_code == 200 and suspended.json()["status"] == "suspended"
        assert (await browser.get(f"{API}/client-portal/me")).status_code == 401
    finally:
        await browser.aclose()


async def test_client_enforced_totp(app: FastAPI, dorg: DOrg) -> None:
    granted = await _grant(dorg, mfa_enforced=True)
    browser = new_client(app)
    try:
        first = await browser.post(
            f"{API}/client-portal/auth/verify-link",
            json={"token": _token(_mails(dorg.tenant_id, "client_portal_invitation")[-1])},
        )
        assert first.json()["status"] == "mfa_enrollment_required"
        secret = first.json()["enrollment"]["secret"]
        assert (await browser.get(f"{API}/client-portal/me")).status_code == 401  # no session yet
        done = await browser.post(f"{API}/client-portal/auth/mfa/verify", json={"code": _totp(secret)})
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "signed_in" and len(done.json()["recovery_codes"]) == 10
        assert (await browser.get(f"{API}/client-portal/me")).status_code == 200
    finally:
        await browser.aclose()
    # Next sign-in asks for a code; a recovery code works once.
    await browser.aclose()
    await dorg.admin.client.post(
        f"{API}/clients/{dorg.client_id}/portal-users/{granted['id']}/resend", headers=dorg.admin.h()
    )
    browser = new_client(app)
    try:
        again = await browser.post(
            f"{API}/client-portal/auth/verify-link",
            json={"token": _token(_mails(dorg.tenant_id, "client_portal_invitation")[-1])},
        )
        assert again.json()["status"] == "mfa_required"
        wrong = await browser.post(f"{API}/client-portal/auth/mfa/verify", json={"code": "000000"})
        assert wrong.status_code == 401
        ok = await browser.post(
            f"{API}/client-portal/auth/mfa/verify", json={"recovery_code": done.json()["recovery_codes"][0]}
        )
        assert ok.status_code == 200 and ok.json()["status"] == "signed_in"
    finally:
        await browser.aclose()


async def test_client_admin_validation_and_isolation(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:  # noqa: F811
    granted = await _grant(dorg)
    dup = await dorg.admin.client.post(
        f"{API}/clients/{dorg.client_id}/portal-users",
        json={"contact_id": str(granted["contact_id"])},
        headers=dorg.admin.h(),
    )
    assert dup.status_code == 409
    restricted = await dorg.admin.client.patch(
        f"{API}/clients/{dorg.client_id}/portal-users/{granted['id']}",
        json={"restricted": True},
        headers=dorg.admin.h(),
    )
    assert restricted.status_code == 422 and restricted.json()["error"]["code"] == "scope_required"
    # Other tenant: 404 everywhere.
    other = other_dorg.admin
    assert (await other.client.get(f"{API}/clients/{dorg.client_id}/portal-users")).status_code == 404
    assert (
        await other.client.post(
            f"{API}/clients/{dorg.client_id}/portal-users/{granted['id']}/suspend", headers=other.h()
        )
    ).status_code == 404
    revoked = await dorg.admin.client.post(
        f"{API}/clients/{dorg.client_id}/portal-users/{granted['id']}/revoke", headers=dorg.admin.h()
    )
    assert revoked.json()["status"] == "revoked"
    again = await dorg.admin.client.post(
        f"{API}/clients/{dorg.client_id}/portal-users/{granted['id']}/reactivate", headers=dorg.admin.h()
    )
    assert again.status_code == 409


# ================================================================ vendor users
async def _invite_vendor(dorg: DOrg, vendor_id: uuid.UUID, *, admin: bool = True) -> tuple[str, str]:
    email = f"vik-{uuid.uuid4().hex[:6]}@vendor.example.com"
    resp = await dorg.admin.client.post(
        f"{API}/vendors/{vendor_id}/portal-users",
        json={"email": email, "display_name": "Vik Vendor", "is_vendor_admin": admin},
        headers=dorg.admin.h(),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"]), email


async def _onboard_vendor_user(
    app: FastAPI, dorg: DOrg, vendor_id: uuid.UUID, *, admin: bool = True
) -> tuple[httpx.AsyncClient, str, str]:
    _vu_id, email = await _invite_vendor(dorg, vendor_id, admin=admin)
    token = _token(_mails(dorg.tenant_id, "vendor_portal_invitation")[-1])
    browser = new_client(app)
    lookup = await browser.post(f"{API}/vendor-portal/auth/invitations/lookup", json={"token": token})
    assert lookup.status_code == 200 and lookup.json()["email"] == email
    weak = await browser.post(
        f"{API}/vendor-portal/auth/invitations/accept", json={"token": token, "password": "short"}
    )
    assert weak.status_code == 422
    accepted = await browser.post(
        f"{API}/vendor-portal/auth/invitations/accept", json={"token": token, "password": STRONG}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "mfa_enrollment_required"
    secret = accepted.json()["enrollment"]["secret"]
    done = await browser.post(f"{API}/vendor-portal/auth/mfa/verify", json={"code": _totp(secret)})
    assert done.status_code == 200 and done.json()["status"] == "signed_in", done.text
    return browser, email, secret


async def test_vendor_invite_password_totp_and_login(app: FastAPI, dorg: DOrg) -> None:
    vendor_id = await make_vendor(dorg.tenant_id)
    browser, email, secret = await _onboard_vendor_user(app, dorg, vendor_id)
    try:
        assert (await browser.get(f"{API}/vendor-portal/me")).json()["org_id"] == str(vendor_id)
        assert (await browser.get(f"{API}/vendor-portal/csrf")).status_code == 204
        out = await browser.post(f"{API}/vendor-portal/auth/logout", headers=_h(browser))
        assert out.status_code == 204
    finally:
        await browser.aclose()

    browser = new_client(app)
    try:
        body = {"tenant_slug": dorg.tenant.slug, "email": email, "password": STRONG}
        wrong = await browser.post(
            f"{API}/vendor-portal/auth/login", json={**body, "password": "Wrong-Password-99"}
        )
        assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "invalid_credentials"
        login = await browser.post(f"{API}/vendor-portal/auth/login", json=body)
        assert login.status_code == 200 and login.json()["status"] == "mfa_required"
        assert (await browser.get(f"{API}/vendor-portal/me")).status_code == 401
        import asyncio

        await asyncio.sleep(0)
        # The enrolment code's time step is spent: use the next one.
        step = mfa_service.current_step(datetime.now(UTC)) + 1
        verify = await browser.post(
            f"{API}/vendor-portal/auth/mfa/verify", json={"code": mfa_service.totp_code(secret, step)}
        )
        assert verify.status_code == 200, verify.text
        assert (await browser.get(f"{API}/vendor-portal/me")).status_code == 200

        # Vendor suspended -> sessions refused.
        async with tenant_session(dorg.tenant_id) as db:
            await db.execute(update(Vendor).where(Vendor.id == vendor_id).values(status="suspended"))
        assert (await browser.get(f"{API}/vendor-portal/me")).status_code == 401
    finally:
        await browser.aclose()


async def test_vendor_self_admin_and_last_admin_guard(app: FastAPI, dorg: DOrg) -> None:
    vendor_id = await make_vendor(dorg.tenant_id)
    browser, _email, _secret = await _onboard_vendor_user(app, dorg, vendor_id)
    try:
        users = await browser.get(f"{API}/vendor-portal/users")
        assert users.status_code == 200 and len(users.json()) == 1
        me_id = users.json()[0]["id"]
        self_off = await browser.post(f"{API}/vendor-portal/users/{me_id}/deactivate", headers=_h(browser))
        assert self_off.status_code == 409 and self_off.json()["error"]["code"] == "last_vendor_admin"
        demote = await browser.patch(
            f"{API}/vendor-portal/users/{me_id}", json={"is_vendor_admin": False}, headers=_h(browser)
        )
        assert demote.status_code == 409
        invited = await browser.post(
            f"{API}/vendor-portal/users",
            json={"email": f"x-{uuid.uuid4().hex[:5]}@vendor.example.com", "display_name": "Xena"},
            headers=_h(browser),
        )
        assert invited.status_code == 201 and invited.json()["status"] == "invited"
        off = await browser.post(
            f"{API}/vendor-portal/users/{invited.json()['id']}/deactivate", headers=_h(browser)
        )
        assert off.json()["status"] == "deactivated"
    finally:
        await browser.aclose()
    # A non-admin vendor user gets the FR-82 403 asking the Account Manager.
    plain = await make_vendor_user(dorg.tenant_id, vendor_id)
    ext = await sign_in_external(app, "vendor_user", plain, dorg.tenant_id, vendor_id)
    try:
        denied = await ext.client.get(f"{API}/vendor-portal/users")
        assert denied.status_code == 403 and denied.json()["error"]["details"]["ask"] == "account_manager"
    finally:
        await ext.client.aclose()


async def test_vendor_password_reset_revokes_sessions(app: FastAPI, dorg: DOrg) -> None:
    vendor_id = await make_vendor(dorg.tenant_id)
    browser, email, _secret = await _onboard_vendor_user(app, dorg, vendor_id)
    try:
        anon = new_client(app)
        try:
            req = await anon.post(
                f"{API}/vendor-portal/auth/password-reset/request",
                json={"tenant_slug": dorg.tenant.slug, "email": email},
            )
            assert req.status_code == 202
            token = _token(_mails(dorg.tenant_id, "vendor_password_reset")[-1])
            done = await anon.post(
                f"{API}/vendor-portal/auth/password-reset/confirm",
                json={"token": token, "password": "Another-Strong-88"},
            )
            assert done.status_code == 200
        finally:
            await anon.aclose()
        assert (await browser.get(f"{API}/vendor-portal/me")).status_code == 401
    finally:
        await browser.aclose()


async def test_vendor_admin_routes_isolation(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:  # noqa: F811
    vendor_id = await make_vendor(dorg.tenant_id)
    vu_id, _email = await _invite_vendor(dorg, vendor_id)
    other = other_dorg.admin
    assert (await other.client.get(f"{API}/vendors/{vendor_id}/portal-users")).status_code == 404
    assert (
        await other.client.post(
            f"{API}/vendors/{vendor_id}/portal-users/{vu_id}/deactivate", headers=other.h()
        )
    ).status_code == 404
    off = await dorg.admin.client.post(
        f"{API}/vendors/{vendor_id}/portal-users/{vu_id}/deactivate", headers=dorg.admin.h()
    )
    assert off.status_code == 200 and off.json()["status"] == "deactivated"


@pytest.mark.parametrize("path", ["/auth/csrf"])
async def test_internal_csrf_reissue(dorg: DOrg, path: str) -> None:
    resp = await dorg.admin.client.get(f"{API}{path}")
    assert resp.status_code == 204 and CSRF_COOKIE in resp.headers.get("set-cookie", "")
    assert csrf(dorg.admin.client)[CSRF_HEADER]
    assert USER_PASSWORD
