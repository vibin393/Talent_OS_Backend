"""Story 22.9 (A10): the CSRF token is bound to the session (HMAC); re-login revokes the incoming session."""

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import platform_session
from app.core.security import CSRF_COOKIE, CSRF_HEADER, USER_COOKIE, csrf_token_for, hash_token
from app.models.identity import Session as AuthSession

from .conftest import USER_PASSWORD, csrf, join, make_tenant, new_client

LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"


async def _login(client: httpx.AsyncClient, slug: str, email: str) -> None:
    resp = await client.post(LOGIN, json={"tenant_slug": slug, "email": email, "password": USER_PASSWORD})
    assert resp.status_code == 200, resp.text


async def test_csrf_cookie_is_hmac_of_session_and_token_a_is_refused_on_session_b(app: FastAPI) -> None:
    tenant = await make_tenant()
    a = await join(app, tenant)
    token_a = a.client.cookies.get(USER_COOKIE) or ""
    assert a.client.cookies.get(CSRF_COOKIE) == csrf_token_for(token_a)

    async with new_client(app) as b:
        await _login(b, a.slug, a.email)
        assert b.cookies.get(CSRF_COOKIE) == csrf_token_for(b.cookies.get(USER_COOKIE) or "")
        # Session B presenting session A's token (cookie + header) is refused.
        stolen = csrf(a.client)[CSRF_HEADER]
        b.cookies.set(CSRF_COOKIE, stolen)
        resp = await b.post(LOGOUT, headers={CSRF_HEADER: stolen})
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "csrf_failed"
    # Session A's own token still works.
    assert (await a.client.post(LOGOUT, headers=csrf(a.client))).status_code == 204
    await a.client.aclose()


async def test_relogin_rotates_csrf_and_revokes_the_incoming_session(app: FastAPI) -> None:
    tenant = await make_tenant()
    m = await join(app, tenant)
    old_session, old_csrf = m.client.cookies.get(USER_COOKIE) or "", m.client.cookies.get(CSRF_COOKIE)
    await _login(m.client, m.slug, m.email)
    assert m.client.cookies.get(USER_COOKIE) != old_session
    assert m.client.cookies.get(CSRF_COOKIE) != old_csrf
    async with platform_session() as db:
        row = (
            await db.execute(select(AuthSession).where(AuthSession.token_hash == hash_token(old_session)))
        ).scalar_one()
        assert row.revoked_at is not None
    # The re-issue endpoint returns the token bound to the current session.
    assert (await m.client.get("/api/v1/auth/csrf")).status_code == 204
    assert m.client.cookies.get(CSRF_COOKIE) == csrf_token_for(m.client.cookies.get(USER_COOKIE) or "")
    await m.client.aclose()
