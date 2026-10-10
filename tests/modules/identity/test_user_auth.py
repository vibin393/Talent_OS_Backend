"""Story 1.3: invitation acceptance, user sign-in/out, sessions, lockout, password reset, tenant context."""

import dataclasses
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.core import audit
from app.core.context import get_context
from app.core.database import DBSessionDep, platform_session, tenant_session
from app.core.mailer import EmailMessage, OutboxMailer, get_mailer
from app.core.passwords import common_passwords, password_policy_violation
from app.core.security import CSRF_COOKIE, CSRF_HEADER, USER_COOKIE, hash_token
from app.core.settings import get_settings
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.main import create_app
from app.models.identity import Invitation, PasswordReset, PlatformOperator, Tenant, User
from app.models.identity import Session as AuthSession
from app.services import auth_service as service

from .conftest import OperatorCreds, csrf, invite, tenant_body, unique_slug

ACCEPT = "/api/v1/auth/invitations/{}/accept"
INVITE = "/api/v1/auth/invitations/{}"
LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"
RESET_REQUEST = "/api/v1/auth/password-reset/request"
RESET_CONFIRM = "/api/v1/auth/password-reset/confirm"
OP_LOGIN = "/api/v1/platform/auth/login"

PASSWORD = "Tangerine-Voyage-42"
NEW_PASSWORD = "Marmalade-Harbour-77"
_FRONTEND = re.escape(get_settings().frontend_url.rstrip("/"))
INVITE_RE = re.compile(_FRONTEND + r"/accept-invite#token=(\S+)")
RESET_RE = re.compile(_FRONTEND + r"/reset-password#token=(\S+)")


# ---------------------------------------------------------------- helpers
@dataclasses.dataclass
class Account:
    tenant_id: uuid.UUID
    slug: str
    email: str
    password: str
    client: httpx.AsyncClient
    user_id: uuid.UUID


def _outbox() -> OutboxMailer:
    mailer = get_mailer()
    assert isinstance(mailer, OutboxMailer)
    return mailer


def _mails(tenant_id: uuid.UUID, kind: str) -> list[EmailMessage]:
    return [m for m in _outbox().sent if m.tenant_id == tenant_id and m.kind == kind]


def _token(mail: EmailMessage, pattern: re.Pattern[str]) -> str:
    match = pattern.search(mail.text)
    assert match, mail.text
    return match.group(1)


async def _tenant(slug: str | None = None, **overrides: Any) -> Tenant:
    async with platform_session() as db:
        tenant = Tenant(
            name="Globex Talent",
            slug=slug or unique_slug("gx"),
            timezone="Asia/Kolkata",
            currency="INR",
            locale="en-IN",
            **{"seat_limit": 10, **overrides},
        )
        db.add(tenant)
        await db.flush()
        await db.refresh(tenant)
        return tenant


async def _invite(tenant_id: uuid.UUID, email: str, role_key: str = "tenant_admin") -> str:
    return await invite(tenant_id, email, role_key)


async def _set_tenant(tenant_id: uuid.UUID, **values: Any) -> None:
    async with platform_session() as db:
        await db.execute(update(Tenant).where(Tenant.id == tenant_id).values(**values))


async def _set_user(tenant_id: uuid.UUID, user_id: uuid.UUID, **values: Any) -> None:
    async with tenant_session(tenant_id) as db:
        await db.execute(update(User).where(User.id == user_id).values(**values))


async def _get_user(tenant_id: uuid.UUID, user_id: uuid.UUID) -> User:
    async with tenant_session(tenant_id) as db:
        user = await db.get(User, user_id)
        assert user is not None
        return user


async def _sessions(user_id: uuid.UUID) -> list[AuthSession]:
    async with platform_session() as db:
        stmt = select(AuthSession).where(AuthSession.principal_id == user_id).order_by(AuthSession.created_at)
        return list((await db.execute(stmt)).scalars())


def _new_client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _signed_up(app: FastAPI, *, slug: str | None = None, **tenant_overrides: Any) -> Account:
    tenant = await _tenant(slug, **tenant_overrides)
    email = f"admin-{uuid.uuid4().hex[:8]}@globex.example.com"
    token = await _invite(tenant.id, email)
    client = _new_client(app)
    resp = await client.post(ACCEPT.format(token), json={"name": "Grace Admin", "password": PASSWORD})
    assert resp.status_code == 201, resp.text
    return Account(
        tenant_id=tenant.id,
        slug=tenant.slug,
        email=email,
        password=PASSWORD,
        client=client,
        user_id=uuid.UUID(resp.json()["user"]["id"]),
    )


@pytest.fixture
async def account(app: FastAPI) -> AsyncIterator[Account]:
    acc = await _signed_up(app)
    yield acc
    await acc.client.aclose()


@pytest.fixture
async def other_account(app: FastAPI) -> AsyncIterator[Account]:
    acc = await _signed_up(app)
    yield acc
    await acc.client.aclose()


async def _age_resets(acc: Account, by: timedelta) -> None:
    async with tenant_session(acc.tenant_id) as db:
        await db.execute(update(PasswordReset).values(created_at=PasswordReset.created_at - by))


def _creds(acc: Account, **overrides: str) -> dict[str, str]:
    body = {"tenant_slug": acc.slug, "email": acc.email, "password": acc.password}
    body.update(overrides)
    return body


@pytest.fixture
def audit_log(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    original = audit.record

    def capture(action: str, entity_type: str | None = None, entity_id: Any = None, **data: Any) -> None:
        events.append((action, {"entity_type": entity_type, "entity_id": entity_id, **data}))
        original(action, entity_type, entity_id, **data)

    monkeypatch.setattr(audit, "record", capture)
    return events


# ---------------------------------------------------------------- acceptance criterion (full flow)
async def test_provisioned_admin_accepts_invite_signs_out_and_back_in(
    op_client: httpx.AsyncClient, client: httpx.AsyncClient, audit_log: list[tuple[str, dict[str, Any]]]
) -> None:
    body = tenant_body()
    created = await op_client.post("/api/v1/platform/tenants", json=body, headers=csrf(op_client))
    assert created.status_code == 201, created.text
    tenant_id = uuid.UUID(created.json()["id"])
    (mail,) = _mails(tenant_id, "tenant_admin_invitation")
    token = _token(mail, INVITE_RE)
    admin_email = str(body["admin_email"]).lower()

    preview = await client.get(INVITE.format(token))
    assert preview.status_code == 200, preview.text
    assert preview.json()["email"] == admin_email
    assert preview.json()["tenant"]["name"] == "Acme Staffing"

    accepted = await client.post(ACCEPT.format(token), json={"name": "Ada Admin", "password": PASSWORD})
    assert accepted.status_code == 201, accepted.text
    user = accepted.json()["user"]
    assert user["email"] == admin_email
    assert user["role_keys"] == ["tenant_admin"]
    assert user["tenant_id"] == str(tenant_id)
    assert accepted.json()["tenant"]["slug"] == body["slug"]
    assert "password_hash" not in user
    assert client.cookies.get(USER_COOKIE) and client.cookies.get(CSRF_COOKIE)
    async with tenant_session(tenant_id) as db:
        (inv,) = (await db.execute(select(Invitation))).scalars()
        assert inv.accepted_at is not None
        db_user = (await db.execute(select(User))).scalar_one()
        assert db_user.password_hash.startswith("$argon2id$")

    assert (await client.get(ME)).json()["user"]["id"] == user["id"]
    assert (await client.post(LOGOUT, headers=csrf(client))).status_code == 204
    assert USER_COOKIE not in client.cookies
    assert (await client.get(ME)).status_code == 401

    login = await client.post(
        LOGIN, json={"tenant_slug": body["slug"], "email": admin_email.upper(), "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["id"] == user["id"]
    assert (await client.get(ME)).status_code == 200

    actions = [a for a, _ in audit_log]
    for expected in ("auth.invitation_accepted", "auth.logout", "auth.login_succeeded"):
        assert expected in actions


# ---------------------------------------------------------------- invitations
async def test_reused_invitation_is_410(client: httpx.AsyncClient) -> None:
    tenant = await _tenant()
    token = await _invite(tenant.id, "reuse@example.com")
    first = await client.post(ACCEPT.format(token), json={"name": "A", "password": PASSWORD})
    assert first.status_code == 201
    again = await client.post(ACCEPT.format(token), json={"name": "B", "password": PASSWORD})
    assert again.status_code == 410
    assert again.json()["error"]["code"] == "invitation_invalid"
    assert (await client.get(INVITE.format(token))).status_code == 410


async def test_expired_invitation_is_410(client: httpx.AsyncClient) -> None:
    tenant = await _tenant()
    token = await _invite(tenant.id, "late@example.com")
    async with tenant_session(tenant.id) as db:
        await db.execute(
            update(Invitation).values(expires_at=datetime.now(UTC) - timedelta(hours=72, seconds=1))
        )
    for resp in (
        await client.get(INVITE.format(token)),
        await client.post(ACCEPT.format(token), json={"name": "Late", "password": PASSWORD}),
    ):
        assert resp.status_code == 410
        assert resp.json()["error"]["code"] == "invitation_invalid"


@pytest.mark.parametrize("token", ["garbage", f"{uuid.uuid4().hex}.{'x' * 43}", f"{'z' * 32}.{'x' * 43}"])
async def test_unknown_or_malformed_invitation_is_410(client: httpx.AsyncClient, token: str) -> None:
    resp = await client.post(ACCEPT.format(token), json={"name": "X", "password": PASSWORD})
    assert resp.status_code == 410
    assert (await client.get(INVITE.format(token))).status_code == 410


async def test_invitation_of_suspended_tenant_is_410(client: httpx.AsyncClient) -> None:
    tenant = await _tenant(status="suspended")
    token = await _invite(tenant.id, "susp@example.com")
    resp = await client.post(ACCEPT.format(token), json={"name": "X", "password": PASSWORD})
    assert resp.status_code == 410


@pytest.mark.parametrize("password", ["Short9chr", "1234567890", "QWERTYUIOP", "          "])
async def test_weak_password_is_422_and_invitation_stays_usable(
    client: httpx.AsyncClient, password: str
) -> None:
    tenant = await _tenant()
    token = await _invite(tenant.id, "weak@example.com")
    resp = await client.post(ACCEPT.format(token), json={"name": "W", "password": password})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "weak_password"
    (field,) = error["details"]["fields"]
    assert field["field"] == "password"
    assert field["type"] == "weak_password"
    assert field["message"]
    ok = await client.post(ACCEPT.format(token), json={"name": "W", "password": PASSWORD})
    assert ok.status_code == 201


async def test_invitation_for_existing_user_is_409_and_not_consumed(account: Account) -> None:
    token = await _invite(account.tenant_id, account.email.upper(), role_key="viewer")
    resp = await account.client.post(ACCEPT.format(token), json={"name": "Dup", "password": PASSWORD})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "user_exists"
    async with tenant_session(account.tenant_id) as db:
        inv = (
            await db.execute(select(Invitation).where(Invitation.token_hash == hash_token(token)))
        ).scalar_one()
        assert inv.accepted_at is None


# ---------------------------------------------------------------- sign-in
async def test_login_ok_returns_profile_tenant_and_cookies(app: FastAPI, account: Account) -> None:
    async with _new_client(app) as c:
        resp = await c.post(LOGIN, json=_creds(account, email=account.email.upper()))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["user"]["email"] == account.email
        assert body["user"]["last_login_at"].endswith("Z")
        assert body["tenant"] == {
            "id": str(account.tenant_id),
            "name": "Globex Talent",
            "slug": account.slug,
            "timezone": "Asia/Kolkata",
            "currency": "INR",
            "locale": "en-IN",
        }
        cookies = resp.headers.get_list("set-cookie")
        session_cookie = next(x for x in cookies if x.startswith(f"{USER_COOKIE}=")).lower()
        assert "httponly" in session_cookie and "samesite=lax" in session_cookie
        token = c.cookies[USER_COOKIE]
    rows = await _sessions(account.user_id)
    row = next(r for r in rows if r.token_hash == hash_token(token))
    assert (row.principal_type, row.tenant_id) == ("user", account.tenant_id)
    assert row.expires_at - row.created_at == timedelta(days=7)


async def test_login_wrong_tenant_or_bad_creds_are_identical_401(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    responses = [
        await client.post(LOGIN, json=_creds(account, tenant_slug=other_account.slug)),
        await client.post(LOGIN, json=_creds(account, tenant_slug=unique_slug("nope"))),
        await client.post(LOGIN, json=_creds(account, password="wrong password!")),
        await client.post(LOGIN, json=_creds(account, email="nobody@globex.example.com")),
    ]
    for resp in responses:
        assert resp.status_code == 401
        assert resp.json() == responses[0].json()
        assert resp.json()["error"]["code"] == "invalid_credentials"
    assert USER_COOKIE not in client.cookies


async def test_lockout_after_five_failures(
    client: httpx.AsyncClient, account: Account, audit_log: list[tuple[str, dict[str, Any]]]
) -> None:
    for _ in range(4):
        resp = await client.post(LOGIN, json=_creds(account, password="wrong password!"))
        assert resp.status_code == 401
        assert resp.json()["error"] == {
            "code": "invalid_credentials",
            "message": "Invalid email or password",
            "details": {},
        }
    fifth = await client.post(LOGIN, json=_creds(account, password="wrong password!"))
    assert (fifth.status_code, fifth.json()["error"]["code"]) == (401, "account_locked")
    assert audit_log[-1][0] == "auth.login_locked"

    # While locked the password is not evaluated: correct and wrong passwords get the identical 401.
    correct = await client.post(LOGIN, json=_creds(account))
    wrong = await client.post(LOGIN, json=_creds(account, password="wrong password!"))
    assert correct.status_code == wrong.status_code == 401
    assert correct.json() == wrong.json() == fifth.json()
    error = correct.json()["error"]
    assert error["code"] == "account_locked"
    locked_until = datetime.fromisoformat(error["details"]["locked_until"].replace("Z", "+00:00"))
    assert timedelta(minutes=14) < locked_until - datetime.now(UTC) <= timedelta(minutes=15)
    assert audit_log[-1][0] == "auth.login_locked"
    user = await _get_user(account.tenant_id, account.user_id)
    assert user.failed_login_count == 0  # attempts while locked are not counted

    await _set_user(account.tenant_id, account.user_id, locked_until=datetime.now(UTC) - timedelta(seconds=1))
    assert (await client.post(LOGIN, json=_creds(account))).status_code == 200
    user = await _get_user(account.tenant_id, account.user_id)
    assert (user.failed_login_count, user.first_failed_at, user.locked_until) == (0, None, None)


async def test_failures_outside_window_do_not_lock(client: httpx.AsyncClient, account: Account) -> None:
    for _ in range(4):
        await client.post(LOGIN, json=_creds(account, password="wrong password!"))
    await _set_user(
        account.tenant_id, account.user_id, first_failed_at=datetime.now(UTC) - timedelta(minutes=16)
    )
    await client.post(LOGIN, json=_creds(account, password="wrong password!"))
    user = await _get_user(account.tenant_id, account.user_id)
    assert user.failed_login_count == 1
    assert user.locked_until is None
    assert (await client.post(LOGIN, json=_creds(account))).status_code == 200


async def test_platform_operator_lockout(client: httpx.AsyncClient, operator: OperatorCreds) -> None:
    for _ in range(5):
        resp = await client.post(OP_LOGIN, json={"email": operator.email, "password": "wrong password!"})
        assert resp.status_code == 401
    resp = await client.post(OP_LOGIN, json={"email": operator.email, "password": operator.password})
    assert resp.status_code == 401
    assert "locked_until" in resp.json()["error"]["details"]
    async with platform_session() as db:
        await db.execute(
            update(PlatformOperator)
            .where(PlatformOperator.id == operator.id)
            .values(locked_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    resp = await client.post(OP_LOGIN, json={"email": operator.email, "password": operator.password})
    assert resp.status_code == 200


async def test_deactivated_user_cannot_login_and_session_is_revoked(
    client: httpx.AsyncClient, account: Account
) -> None:
    assert (await account.client.get(ME)).status_code == 200
    await _set_user(account.tenant_id, account.user_id, is_active=False)
    resp = await client.post(LOGIN, json=_creds(account))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_credentials"
    me = await account.client.get(ME)
    assert me.status_code == 401
    assert me.json()["error"]["code"] == "session_expired"
    assert all(s.revoked_at is not None for s in await _sessions(account.user_id))


async def test_suspended_tenant_blocks_login_and_existing_sessions(
    client: httpx.AsyncClient, account: Account
) -> None:
    assert (await account.client.get(ME)).status_code == 200
    await _set_tenant(account.tenant_id, status="suspended")
    # Epic 12 (FR-70): a suspended workspace answers 403 ``tenant_suspended`` at sign-in (before hashing).
    login = await client.post(LOGIN, json=_creds(account))
    assert (login.status_code, login.json()["error"]["code"]) == (403, "tenant_suspended")
    assert (await account.client.get(ME)).status_code == 401


# ---------------------------------------------------------------- sessions
async def test_no_or_forged_cookie_is_401(client: httpx.AsyncClient) -> None:
    resp = await client.get(ME)
    assert (resp.status_code, resp.json()["error"]["code"]) == (401, "unauthorized")
    client.cookies.set(USER_COOKIE, "forged")
    resp = await client.get(ME)
    assert (resp.status_code, resp.json()["error"]["code"]) == (401, "session_expired")
    cleared = [c.lower() for c in resp.headers.get_list("set-cookie") if c.startswith(f"{USER_COOKIE}=")]
    assert cleared and "max-age=0" in cleared[0]


@pytest.mark.parametrize(
    ("column", "age"),
    [
        ("last_seen_at", timedelta(hours=8, minutes=1)),
        ("expires_at", timedelta(seconds=1)),
        ("revoked_at", timedelta(0)),
    ],
)
async def test_stale_user_session_is_rejected(account: Account, column: str, age: timedelta) -> None:
    async with platform_session() as db:
        await db.execute(
            update(AuthSession)
            .where(AuthSession.principal_id == account.user_id)
            .values({column: datetime.now(UTC) - age})
        )
    resp = await account.client.get(ME)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "session_expired"


async def test_tenant_session_overrides(app: FastAPI) -> None:
    acc = await _signed_up(app, session_idle_minutes=30, session_absolute_hours=2)
    try:
        (row,) = await _sessions(acc.user_id)
        assert row.expires_at - row.created_at == timedelta(hours=2)
        async with platform_session() as db:
            await db.execute(
                update(AuthSession)
                .where(AuthSession.id == row.id)
                .values(last_seen_at=datetime.now(UTC) - timedelta(minutes=29))
            )
        assert (await acc.client.get(ME)).status_code == 200  # within idle; touched
        async with platform_session() as db:
            await db.execute(
                update(AuthSession)
                .where(AuthSession.id == row.id)
                .values(last_seen_at=datetime.now(UTC) - timedelta(minutes=31))
            )
        assert (await acc.client.get(ME)).status_code == 401
    finally:
        await acc.client.aclose()


async def test_logout_requires_csrf_and_revokes(account: Account) -> None:
    missing = await account.client.post(LOGOUT)
    assert (missing.status_code, missing.json()["error"]["code"]) == (403, "csrf_failed")
    assert (await account.client.post(LOGOUT, headers={CSRF_HEADER: "nope"})).status_code == 403
    assert (await account.client.post(LOGOUT, headers=csrf(account.client))).status_code == 204
    assert all(s.revoked_at is not None for s in await _sessions(account.user_id))


# ---------------------------------------------------------------- request context + tenant DB
@pytest.fixture
async def ctx_client() -> AsyncIterator[FastAPI]:
    app = create_app()

    @app.post("/api/v1/_test/mutate")
    async def mutate(principal: CurrentUserDep) -> dict[str, str]:
        return {"id": str(principal.id)}

    @app.get("/api/v1/_test/wrong-db")
    async def wrong_db(_: CurrentUserDep, db: DBSessionDep) -> int:
        return len((await db.execute(select(User.id))).all())

    @app.get("/api/v1/_test/whoami")
    async def whoami(principal: CurrentUserDep, db: TenantDBSessionDep) -> dict[str, Any]:
        ctx = get_context()
        emails = list((await db.execute(select(User.email))).scalars())
        tenants = list((await db.execute(select(func.count()).select_from(Invitation))).scalars())
        return {
            "principal_id": str(ctx.principal.id),
            "principal_type": ctx.principal.type,
            "ctx_tenant_id": str(ctx.tenant_id),
            "principal_tenant_id": str(principal.tenant_id),
            "emails": emails,
            "invitations": tenants[0],
        }

    yield app


async def test_authenticated_request_sets_context_and_rls_scoped_db(
    ctx_client: FastAPI, account: Account, other_account: Account
) -> None:
    async with _new_client(ctx_client) as c:
        c.cookies.update(dict(account.client.cookies))
        resp = await c.get("/api/v1/_test/whoami")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["principal_id"] == str(account.user_id)
    assert body["principal_type"] == "user"
    assert body["ctx_tenant_id"] == body["principal_tenant_id"] == str(account.tenant_id)
    assert body["emails"] == [account.email]  # other_account's user is invisible (RLS)
    assert body["invitations"] == 1
    assert other_account.email not in body["emails"]


async def test_tenant_db_dependency_requires_authentication(ctx_client: FastAPI) -> None:
    async with _new_client(ctx_client) as c:
        assert (await c.get("/api/v1/_test/whoami")).status_code == 401


# ---------------------------------------------------------------- cross-tenant
async def test_session_of_tenant_a_cannot_read_tenant_b(account: Account, other_account: Account) -> None:
    me = await account.client.get(ME)
    assert me.json()["tenant"]["id"] == str(account.tenant_id)
    assert me.json()["user"]["id"] == str(account.user_id)

    # A session row re-pointed at tenant B cannot resolve A's user under B's RLS scope.
    async with platform_session() as db:
        await db.execute(
            update(AuthSession)
            .where(AuthSession.principal_id == account.user_id)
            .values(tenant_id=other_account.tenant_id)
        )
    resp = await account.client.get(ME)
    assert resp.status_code == 401
    assert (await other_account.client.get(ME)).json()["tenant"]["id"] == str(other_account.tenant_id)


async def test_cross_tenant_auth_endpoints(
    client: httpx.AsyncClient, account: Account, other_account: Account
) -> None:
    # A's credentials never open B.
    assert (await client.post(LOGIN, json=_creds(account, tenant_slug=other_account.slug))).status_code == 401
    # A reset request against B's slug sends nothing.
    before = len(_outbox().sent)
    resp = await client.post(RESET_REQUEST, json={"tenant_slug": other_account.slug, "email": account.email})
    assert resp.status_code == 202
    assert len(_outbox().sent) == before
    # A's reset token re-prefixed with B's tenant id is unknown there.
    await client.post(RESET_REQUEST, json={"tenant_slug": account.slug, "email": account.email})
    (mail,) = _mails(account.tenant_id, "password_reset")
    token = _token(mail, RESET_RE)
    forged = f"{other_account.tenant_id.hex}.{token.partition('.')[2]}"
    resp = await client.post(RESET_CONFIRM, json={"token": forged, "password": NEW_PASSWORD})
    assert resp.status_code == 410
    # A's invitation token re-prefixed with B's tenant id is unknown there.
    inv = await _invite(account.tenant_id, "later@example.com")
    forged_inv = f"{other_account.tenant_id.hex}.{inv.partition('.')[2]}"
    assert (await client.get(INVITE.format(forged_inv))).status_code == 410


# ---------------------------------------------------------------- password reset
async def test_password_reset_flow(
    app: FastAPI, client: httpx.AsyncClient, account: Account, audit_log: list[tuple[str, dict[str, Any]]]
) -> None:
    async with _new_client(app) as second:
        assert (await second.post(LOGIN, json=_creds(account))).status_code == 200
        assert len(await _sessions(account.user_id)) == 2

        resp = await client.post(
            RESET_REQUEST, json={"tenant_slug": account.slug.upper(), "email": account.email.upper()}
        )
        assert resp.status_code == 202
        (mail,) = _mails(account.tenant_id, "password_reset")
        assert mail.to == account.email
        token = _token(mail, RESET_RE)
        async with tenant_session(account.tenant_id) as db:
            (row,) = (await db.execute(select(PasswordReset))).scalars()
            assert row.token_hash == hash_token(token)
            assert timedelta(minutes=59) < row.expires_at - datetime.now(UTC) <= timedelta(hours=1)

        weak = await client.post(RESET_CONFIRM, json={"token": token, "password": "short"})
        assert weak.status_code == 422
        assert weak.json()["error"]["details"]["fields"][0]["field"] == "password"

        done = await client.post(RESET_CONFIRM, json={"token": token, "password": NEW_PASSWORD})
        assert done.status_code == 204
        assert all(s.revoked_at is not None for s in await _sessions(account.user_id))
        assert (await account.client.get(ME)).status_code == 401
        assert (await second.get(ME)).status_code == 401

    reused = await client.post(RESET_CONFIRM, json={"token": token, "password": "Another-Password-99"})
    assert (reused.status_code, reused.json()["error"]["code"]) == (410, "reset_invalid")
    assert (await client.post(LOGIN, json=_creds(account))).status_code == 401
    assert (await client.post(LOGIN, json=_creds(account, password=NEW_PASSWORD))).status_code == 200
    actions = [a for a, _ in audit_log]
    assert "auth.password_reset_requested" in actions
    assert "auth.password_reset_completed" in actions


async def test_reset_request_unknown_email_or_tenant_is_same_202(
    client: httpx.AsyncClient, account: Account
) -> None:
    before = len(_outbox().sent)
    unknown_email = await client.post(
        RESET_REQUEST, json={"tenant_slug": account.slug, "email": "ghost@example.com"}
    )
    unknown_tenant = await client.post(
        RESET_REQUEST, json={"tenant_slug": unique_slug("ghost"), "email": account.email}
    )
    known = await client.post(RESET_REQUEST, json={"tenant_slug": account.slug, "email": account.email})
    assert unknown_email.status_code == unknown_tenant.status_code == known.status_code == 202
    assert unknown_email.content == unknown_tenant.content == known.content
    assert len(_outbox().sent) == before + 1


async def test_reset_link_expired_or_superseded_is_410(client: httpx.AsyncClient, account: Account) -> None:
    body = {"tenant_slug": account.slug, "email": account.email}
    await client.post(RESET_REQUEST, json=body)
    await _age_resets(account, timedelta(seconds=61))  # past the cooldown
    await client.post(RESET_REQUEST, json=body)
    first, second = (_token(m, RESET_RE) for m in _mails(account.tenant_id, "password_reset"))
    superseded = await client.post(RESET_CONFIRM, json={"token": first, "password": NEW_PASSWORD})
    assert superseded.status_code == 410
    async with tenant_session(account.tenant_id) as db:
        await db.execute(
            update(PasswordReset)
            .where(PasswordReset.token_hash == hash_token(second))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    expired = await client.post(RESET_CONFIRM, json={"token": second, "password": NEW_PASSWORD})
    assert expired.status_code == 410
    garbage = await client.post(RESET_CONFIRM, json={"token": "garbage", "password": NEW_PASSWORD})
    assert garbage.status_code == 410


async def test_reset_clears_lockout(client: httpx.AsyncClient, account: Account) -> None:
    for _ in range(5):
        await client.post(LOGIN, json=_creds(account, password="wrong password!"))
    await client.post(RESET_REQUEST, json={"tenant_slug": account.slug, "email": account.email})
    (mail,) = _mails(account.tenant_id, "password_reset")
    token = _token(mail, RESET_RE)
    assert (
        await client.post(RESET_CONFIRM, json={"token": token, "password": NEW_PASSWORD})
    ).status_code == 204
    assert (await client.post(LOGIN, json=_creds(account, password=NEW_PASSWORD))).status_code == 200


async def test_reset_for_deactivated_user_sends_nothing(client: httpx.AsyncClient, account: Account) -> None:
    await _set_user(account.tenant_id, account.user_id, is_active=False)
    resp = await client.post(RESET_REQUEST, json={"tenant_slug": account.slug, "email": account.email})
    assert resp.status_code == 202
    assert _mails(account.tenant_id, "password_reset") == []


# ---------------------------------------------------------------- units
def test_password_policy() -> None:
    assert len(common_passwords()) >= 9000
    assert password_policy_violation("a" * 9) is not None
    assert password_policy_violation("   short9   ") is not None  # padding does not count
    assert password_policy_violation(" " * 12) is not None
    assert password_policy_violation("1234567890") is not None
    assert password_policy_violation("QwErTyUiOp") is not None  # case-insensitive
    assert password_policy_violation(PASSWORD) is None


def test_check_login_attempt_unknown_account() -> None:
    check = service.check_login_attempt(
        None, password_hash=None, is_active=False, password=PASSWORD, now=datetime.now(UTC)
    )
    assert check.outcome == "failed"
    assert check.error().details == {}


async def test_audit_record_persists_platform_event() -> None:
    thing = uuid.uuid4()
    async with platform_session() as db:
        audit.record("test.event", "thing", thing, session=db, extra=uuid.uuid4())
    async with platform_session() as db:
        row = (
            await db.execute(select(audit.AuditLog).where(audit.AuditLog.entity_id == str(thing)))
        ).scalar_one()
    assert (row.action, row.tenant_id, row.actor_type) == ("test.event", None, "system")


async def test_audit_record_refuses_tenant_mismatch() -> None:
    async with platform_session() as db:
        with pytest.raises(ValueError, match="tenant"):
            audit.record("test.event", "thing", uuid.uuid4(), session=db, tenant_id=uuid.uuid4())


# ---------------------------------------------------------------- review follow-ups
async def test_locked_state_is_never_revealed_for_deactivated_user(
    client: httpx.AsyncClient, account: Account
) -> None:
    await _set_user(
        account.tenant_id,
        account.user_id,
        is_active=False,
        locked_until=datetime.now(UTC) + timedelta(minutes=10),
    )
    resp = await client.post(LOGIN, json=_creds(account))
    assert (resp.status_code, resp.json()["error"]["code"]) == (401, "invalid_credentials")
    assert resp.json()["error"]["details"] == {}


async def test_seat_limit_blocks_invitation_acceptance(app: FastAPI, client: httpx.AsyncClient) -> None:
    acc = await _signed_up(app, seat_limit=1)
    try:
        token = await _invite(acc.tenant_id, "second@example.com", role_key="viewer")
        resp = await client.post(ACCEPT.format(token), json={"name": "Second", "password": PASSWORD})
        assert (resp.status_code, resp.json()["error"]["code"]) == (409, "seat_limit_reached")
        async with tenant_session(acc.tenant_id) as db:
            inv = (
                await db.execute(select(Invitation).where(Invitation.token_hash == hash_token(token)))
            ).scalar_one()
            assert inv.accepted_at is None
            assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 1
        # A deactivated user frees the seat.
        await _set_user(acc.tenant_id, acc.user_id, is_active=False)
        ok = await client.post(ACCEPT.format(token), json={"name": "Second", "password": PASSWORD})
        assert ok.status_code == 201
    finally:
        await acc.client.aclose()


async def test_user_email_race_maps_to_409(account: Account, monkeypatch: pytest.MonkeyPatch) -> None:
    token = await _invite(account.tenant_id, account.email, role_key="viewer")
    original_select = sa.select

    def blind_select(*args: object) -> object:
        stmt = original_select(*args)  # type: ignore[call-overload]
        if args and args[0] is User.id:
            return stmt.where(text("false"))
        return stmt

    monkeypatch.setattr("app.services.auth_service.select", blind_select)
    resp = await account.client.post(ACCEPT.format(token), json={"name": "Dup", "password": PASSWORD})
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "user_exists")


async def test_reset_request_with_failing_mailer_is_still_202(
    client: httpx.AsyncClient, account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenMailer:
        def send(self, message: EmailMessage) -> None:
            raise OSError("smtp down")

    monkeypatch.setattr("app.core.mailer.get_mailer", BrokenMailer)
    resp = await client.post(RESET_REQUEST, json={"tenant_slug": account.slug, "email": account.email})
    assert resp.status_code == 202
    async with tenant_session(account.tenant_id) as db:
        assert len(list((await db.execute(select(PasswordReset))).scalars())) == 1


async def test_reset_requests_are_throttled(client: httpx.AsyncClient, account: Account) -> None:
    body = {"tenant_slug": account.slug, "email": account.email}
    first = await client.post(RESET_REQUEST, json=body)
    second = await client.post(RESET_REQUEST, json=body)
    assert first.status_code == second.status_code == 202
    assert first.content == second.content
    assert len(_mails(account.tenant_id, "password_reset")) == 1
    async with tenant_session(account.tenant_id) as db:
        (row,) = (await db.execute(select(PasswordReset))).scalars()
        assert row.used_at is None  # the first link stays valid


async def test_csrf_enforced_on_user_mutation_and_failure_does_not_touch_session(
    ctx_client: FastAPI, account: Account
) -> None:
    two_minutes_ago = datetime.now(UTC) - timedelta(minutes=2)
    async with platform_session() as db:
        await db.execute(
            update(AuthSession)
            .where(AuthSession.principal_id == account.user_id)
            .values(last_seen_at=two_minutes_ago)
        )
    async with _new_client(ctx_client) as c:
        c.cookies.update(dict(account.client.cookies))
        missing = await c.post("/api/v1/_test/mutate")
        assert (missing.status_code, missing.json()["error"]["code"]) == (403, "csrf_failed")
        wrong = await c.post("/api/v1/_test/mutate", headers={CSRF_HEADER: "nope"})
        assert wrong.status_code == 403
        (row,) = await _sessions(account.user_id)
        assert row.last_seen_at == two_minutes_ago
        ok = await c.post("/api/v1/_test/mutate", headers=csrf(c))
        assert ok.status_code == 200
        (row,) = await _sessions(account.user_id)
        assert row.last_seen_at > two_minutes_ago


async def test_lowering_tenant_absolute_limit_expires_existing_sessions(account: Account) -> None:
    async with platform_session() as db:
        await db.execute(
            update(AuthSession)
            .where(AuthSession.principal_id == account.user_id)
            .values(created_at=datetime.now(UTC) - timedelta(hours=3))
        )
    assert (await account.client.get(ME)).status_code == 200  # default 7 d
    await _set_tenant(account.tenant_id, session_absolute_hours=2)
    resp = await account.client.get(ME)
    assert (resp.status_code, resp.json()["error"]["code"]) == (401, "session_expired")


@pytest.mark.parametrize(
    ("column", "value"),
    [("session_idle_minutes", 1441), ("session_absolute_hours", 721), ("session_idle_minutes", 0)],
)
async def test_session_override_bounds(column: str, value: int) -> None:
    with pytest.raises(IntegrityError):
        await _tenant(None, **{column: value})


async def test_get_db_refuses_user_routes(ctx_client: FastAPI, account: Account) -> None:
    async with _new_client(ctx_client) as c:
        c.cookies.update(dict(account.client.cookies))
        resp = await c.get("/api/v1/_test/wrong-db")
    assert resp.status_code == 500
