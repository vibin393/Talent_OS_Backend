"""Operator sign-in/out, platform sessions (idle / absolute / revoked) and CSRF."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.core.database import platform_session
from app.core.security import CSRF_COOKIE, PLATFORM_COOKIE, hash_token
from app.models.identity import PlatformOperator
from app.models.identity import Session as AuthSession
from app.services import auth_service as service

from .conftest import OperatorCreds, csrf

LOGIN = "/api/v1/platform/auth/login"
LOGOUT = "/api/v1/platform/auth/logout"
ME = "/api/v1/platform/auth/me"
TENANTS = "/api/v1/platform/tenants"


def _set_cookies(resp: httpx.Response) -> list[str]:
    return resp.headers.get_list("set-cookie")


def _cookie_header(resp: httpx.Response, name: str) -> str:
    matches = [c for c in _set_cookies(resp) if c.startswith(f"{name}=")]
    assert matches, f"no Set-Cookie for {name}: {_set_cookies(resp)}"
    return matches[0]


async def _sessions_for(operator_id: uuid.UUID) -> list[AuthSession]:
    async with platform_session() as db:
        stmt = select(AuthSession).where(AuthSession.principal_id == operator_id)
        return list((await db.execute(stmt)).scalars())


async def test_login_ok_sets_cookies_and_returns_profile(
    client: httpx.AsyncClient, operator: OperatorCreds
) -> None:
    resp = await client.post(LOGIN, json={"email": operator.email.upper(), "password": operator.password})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(operator.id)
    assert body["email"] == operator.email
    assert body["mfa_enabled"] is False
    assert body["last_login_at"].endswith("Z")
    assert "password_hash" not in body and "mfa_secret" not in body

    session_cookie = _cookie_header(resp, PLATFORM_COOKIE).lower()
    assert "httponly" in session_cookie
    assert "samesite=lax" in session_cookie
    assert "secure" not in session_cookie  # app_env != production
    csrf_cookie = _cookie_header(resp, CSRF_COOKIE).lower()
    assert "httponly" not in csrf_cookie

    # Only the SHA-256 of the cookie token is stored.
    token = client.cookies[PLATFORM_COOKIE]
    assert len(token) >= 43  # 32 random bytes
    rows = await _sessions_for(operator.id)
    assert [r.token_hash for r in rows] == [hash_token(token)]
    assert rows[0].principal_type == "platform"
    assert rows[0].tenant_id is None
    assert rows[0].expires_at - rows[0].created_at == timedelta(days=7)


async def test_login_secure_cookie_in_production(
    client: httpx.AsyncClient, operator: OperatorCreds, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import security

    monkeypatch.setattr(security, "_secure", lambda: True)
    resp = await client.post(LOGIN, json={"email": operator.email, "password": operator.password})
    assert resp.status_code == 200
    assert "secure" in _cookie_header(resp, PLATFORM_COOKIE).lower()
    assert "secure" in _cookie_header(resp, CSRF_COOKIE).lower()


async def test_bad_password_and_unknown_email_are_identical(
    client: httpx.AsyncClient, operator: OperatorCreds
) -> None:
    wrong = await client.post(LOGIN, json={"email": operator.email, "password": "not the password"})
    unknown = await client.post(
        LOGIN, json={"email": f"nobody-{uuid.uuid4().hex}@example.com", "password": operator.password}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["error"]["code"] == "invalid_credentials"
    assert PLATFORM_COOKIE not in client.cookies
    assert await _sessions_for(operator.id) == []


async def test_inactive_operator_cannot_login(client: httpx.AsyncClient, operator: OperatorCreds) -> None:
    async with platform_session() as db:
        await db.execute(
            update(PlatformOperator).where(PlatformOperator.id == operator.id).values(is_active=False)
        )
    resp = await client.post(LOGIN, json={"email": operator.email, "password": operator.password})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_credentials"


async def test_me_returns_profile(op_client: httpx.AsyncClient, operator: OperatorCreds) -> None:
    resp = await op_client.get(ME)
    assert resp.status_code == 200
    assert resp.json()["email"] == operator.email


async def test_no_session_is_401(client: httpx.AsyncClient) -> None:
    for path in (ME, TENANTS, f"{TENANTS}/{uuid.uuid4()}"):
        resp = await client.get(path)
        assert resp.status_code == 401, path
        assert resp.json()["error"]["code"] == "unauthorized"
    resp = await client.post(TENANTS, json={})
    assert resp.status_code == 401


async def test_unknown_session_token_is_401_and_clears_cookie(client: httpx.AsyncClient) -> None:
    client.cookies.set(PLATFORM_COOKIE, "forged-token")
    resp = await client.get(ME)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "session_expired"
    assert "max-age=0" in _cookie_header(resp, PLATFORM_COOKIE).lower()
    assert "max-age=0" in _cookie_header(resp, CSRF_COOKIE).lower()


@pytest.mark.parametrize(
    ("column", "age"),
    [
        ("last_seen_at", timedelta(hours=8, minutes=1)),  # idle > 8 h
        ("expires_at", timedelta(seconds=1)),  # absolute > 7 d (expires_at in the past)
        ("revoked_at", timedelta(0)),  # revoked
    ],
)
async def test_stale_session_is_rejected_and_cookie_cleared(
    op_client: httpx.AsyncClient, operator: OperatorCreds, column: str, age: timedelta
) -> None:
    assert (await op_client.get(ME)).status_code == 200
    async with platform_session() as db:
        await db.execute(
            update(AuthSession)
            .where(AuthSession.principal_id == operator.id)
            .values({column: datetime.now(UTC) - age})
        )
    resp = await op_client.get(ME)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "session_expired"
    assert "max-age=0" in _cookie_header(resp, PLATFORM_COOKIE).lower()
    assert PLATFORM_COOKIE not in op_client.cookies


async def test_session_within_idle_window_is_accepted_and_touched(
    op_client: httpx.AsyncClient, operator: OperatorCreds
) -> None:
    seven_hours_ago = datetime.now(UTC) - timedelta(hours=7)
    async with platform_session() as db:
        await db.execute(
            update(AuthSession)
            .where(AuthSession.principal_id == operator.id)
            .values(last_seen_at=seven_hours_ago)
        )
    assert (await op_client.get(ME)).status_code == 200
    (row,) = await _sessions_for(operator.id)
    assert row.last_seen_at > seven_hours_ago + timedelta(hours=6)


async def test_deactivated_operator_session_is_rejected(
    op_client: httpx.AsyncClient, operator: OperatorCreds
) -> None:
    async with platform_session() as db:
        await db.execute(
            update(PlatformOperator).where(PlatformOperator.id == operator.id).values(is_active=False)
        )
    assert (await op_client.get(ME)).status_code == 401
    (row,) = await _sessions_for(operator.id)
    assert row.revoked_at is not None


async def test_missing_or_wrong_csrf_is_403(op_client: httpx.AsyncClient) -> None:
    body = {"name": "X", "slug": "csrf-check", "seat_limit": 1, "admin_email": "a@example.com"}
    missing = await op_client.post(TENANTS, json=body)
    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "csrf_failed"

    wrong = await op_client.post(TENANTS, json=body, headers={"X-CSRF-Token": "nope"})
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "csrf_failed"

    logout = await op_client.post(LOGOUT)
    assert logout.status_code == 403
    assert (await op_client.get(ME)).status_code == 200  # safe methods need no CSRF header


async def test_logout_revokes_session_and_clears_cookies(
    op_client: httpx.AsyncClient, operator: OperatorCreds
) -> None:
    token = op_client.cookies[PLATFORM_COOKIE]
    resp = await op_client.post(LOGOUT, headers=csrf(op_client))
    assert resp.status_code == 204
    assert "max-age=0" in _cookie_header(resp, PLATFORM_COOKIE).lower()
    assert "max-age=0" in _cookie_header(resp, CSRF_COOKIE).lower()
    (row,) = await _sessions_for(operator.id)
    assert row.revoked_at is not None

    # Replaying the old cookie is rejected.
    op_client.cookies.set(PLATFORM_COOKIE, token)
    assert (await op_client.get(ME)).status_code == 401


async def test_resolve_session_is_scoped_to_principal_type(operator: OperatorCreds) -> None:
    async with platform_session() as db:
        issued = await service.create_session(db, principal_type="platform", principal_id=operator.id)
    async with platform_session() as db:
        assert await service.resolve_session(db, "user", issued.token) is None
        assert await service.resolve_session(db, "candidate", issued.token) is None
        assert (await service.resolve_session(db, "platform", issued.token)) is not None
        assert await service.resolve_session(db, "platform", "") is None


async def test_revoke_principal_sessions(operator: OperatorCreds) -> None:
    async with platform_session() as db:
        a = await service.create_session(db, principal_type="platform", principal_id=operator.id)
        b = await service.create_session(db, principal_type="platform", principal_id=operator.id)
    async with platform_session() as db:
        assert await service.revoke_principal_sessions(db, "platform", operator.id) == 2
    async with platform_session() as db:
        assert await service.resolve_session(db, "platform", a.token) is None
        assert await service.resolve_session(db, "platform", b.token) is None


async def test_mfa_enabled_operator_gets_a_challenge_not_a_session(
    client: httpx.AsyncClient, operator: OperatorCreds
) -> None:
    """Story 1.4 replaced the story 1.2 refusal with the MFA challenge flow (see test_mfa.py)."""
    async with platform_session() as db:
        await db.execute(
            update(PlatformOperator).where(PlatformOperator.id == operator.id).values(mfa_enabled=True)
        )
    resp = await client.post(LOGIN, json={"email": operator.email, "password": operator.password})
    assert resp.status_code == 200
    assert resp.json() == {"mfa_required": True}
    assert "tos_mfa" in client.cookies
    assert PLATFORM_COOKIE not in client.cookies
    assert await _sessions_for(operator.id) == []


async def test_login_rehashes_outdated_password_hash(
    client: httpx.AsyncClient, operator: OperatorCreds
) -> None:
    from argon2 import PasswordHasher

    from app.core.security import password_needs_rehash, verify_password

    old_hash = PasswordHasher(time_cost=1).hash(operator.password)
    assert password_needs_rehash(old_hash)
    async with platform_session() as db:
        await db.execute(
            update(PlatformOperator).where(PlatformOperator.id == operator.id).values(password_hash=old_hash)
        )
    resp = await client.post(LOGIN, json={"email": operator.email, "password": operator.password})
    assert resp.status_code == 200
    async with platform_session() as db:
        new_hash = (await db.get(PlatformOperator, operator.id)).password_hash  # type: ignore[union-attr]
    assert new_hash != old_hash
    assert not password_needs_rehash(new_hash)
    assert verify_password(new_hash, operator.password)
