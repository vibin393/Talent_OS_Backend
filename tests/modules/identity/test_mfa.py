"""Story 1.4: TOTP MFA -- enrolment, challenge sign-in, replay protection, recovery codes, attempt limit,
tenant "require MFA" restricted sessions, disable/regenerate, platform operators, audit and encryption."""

import contextlib
import dataclasses
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core import audit
from app.core.database import platform_session, tenant_session
from app.core.security import MFA_COOKIE, PLATFORM_COOKIE, USER_COOKIE, decrypt_str
from app.models.identity import MfaChallenge, MfaRecoveryCode, PlatformOperator, User
from app.services import mfa_service as mfa

from .conftest import USER_PASSWORD, Member, OperatorCreds, Org, csrf, join, new_client

LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"
LOGOUT = "/api/v1/auth/logout"
ENROLL = "/api/v1/auth/mfa/enroll"
CONFIRM = "/api/v1/auth/mfa/enroll/confirm"
VERIFY = "/api/v1/auth/mfa/verify"
DISABLE = "/api/v1/auth/mfa/disable"
REGENERATE = "/api/v1/auth/mfa/recovery-codes/regenerate"
SECURITY = "/api/v1/tenant/security"
TEAMS = "/api/v1/admin/teams"
OP_LOGIN = "/api/v1/platform/auth/login"
OP = "/api/v1/platform/auth/mfa"
RECOVERY_RE = re.compile(r"^[A-HJ-NP-Z2-9]{4}(-[A-HJ-NP-Z2-9]{4}){3}$")


@dataclasses.dataclass
class Clock:
    now: datetime

    def step(self) -> int:
        return mfa.current_step(self.now)

    def tick(self, seconds: int = 30) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock(datetime.now(UTC))
    monkeypatch.setattr(mfa, "_now", lambda: c.now)
    return c


@pytest.fixture
async def member(app: FastAPI, org: Org) -> AsyncIterator[Member]:
    m = await join(app, org.tenant, "recruiter", name="Mo Member")
    yield m
    await m.client.aclose()


def _code(secret: str, clock: Clock) -> str:
    return mfa.totp_code(secret, clock.step())


async def _enroll(m: Member, clock: Clock) -> tuple[str, list[str]]:
    """Enrol ``m`` (at the clock's current step); returns (secret, recovery codes)."""
    start = await m.client.post(ENROLL, headers=m.h())
    assert start.status_code == 200, start.text
    secret = start.json()["secret"]
    confirm = await m.client.post(CONFIRM, json={"code": _code(secret, clock)}, headers=m.h())
    assert confirm.status_code == 200, confirm.text
    return secret, confirm.json()["recovery_codes"]


@contextlib.asynccontextmanager
async def _password_step(app: FastAPI, m: Member) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a fresh client past the password step (it holds only the ``tos_mfa`` challenge cookie)."""
    async with new_client(app) as client:
        resp = await client.post(
            LOGIN, json={"tenant_slug": m.slug, "email": m.email, "password": USER_PASSWORD}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"mfa_required": True}
        assert MFA_COOKIE in client.cookies
        assert USER_COOKIE not in client.cookies
        yield client


async def _actions(tenant_id: uuid.UUID, entity_id: uuid.UUID) -> list[str]:
    async with tenant_session(tenant_id) as db:
        rows = await db.execute(
            select(audit.AuditLog.action)
            .where(audit.AuditLog.entity_id == str(entity_id))
            .order_by(audit.AuditLog.occurred_at)
        )
        return list(rows.scalars())


# ---------------------------------------------------------------- enrolment
async def test_enrol_start_confirm_and_encryption_at_rest(member: Member, clock: Clock, org: Org) -> None:
    start = await member.client.post(ENROLL, headers=member.h())
    assert start.status_code == 200, start.text
    body = start.json()
    assert re.fullmatch(r"[A-Z2-7]{32}", body["secret"])
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    assert "issuer=Talent%20OS" in body["otpauth_uri"]
    me = (await member.client.get(ME)).json()
    assert me["user"]["mfa_enabled"] is False  # pending until confirmed

    # Restarting a pending enrolment rotates the secret.
    secret = (await member.client.post(ENROLL, headers=member.h())).json()["secret"]
    assert secret != body["secret"]

    wrong = await member.client.post(CONFIRM, json={"code": "000000"}, headers=member.h())
    assert (wrong.status_code, wrong.json()["error"]["code"]) == (422, "invalid_code")

    confirm = await member.client.post(CONFIRM, json={"code": _code(secret, clock)}, headers=member.h())
    assert confirm.status_code == 200, confirm.text
    codes = confirm.json()["recovery_codes"]
    assert len(codes) == 10 == len(set(codes))
    assert all(RECOVERY_RE.fullmatch(c) for c in codes)
    assert (await member.client.get(ME)).json()["user"]["mfa_enabled"] is True

    again = await member.client.post(ENROLL, headers=member.h())
    assert (again.status_code, again.json()["error"]["code"]) == (409, "mfa_already_enabled")

    async with tenant_session(org.tenant.id) as db:
        user = await db.get(User, member.user_id)
        assert user is not None
        assert user.mfa_secret_encrypted is not None
        assert secret not in user.mfa_secret_encrypted
        assert decrypt_str("mfa", user.mfa_secret_encrypted) == secret
        stored = (
            await db.execute(select(MfaRecoveryCode.code_hash).where(MfaRecoveryCode.principal_id == user.id))
        ).scalars()
        assert set(stored) == {mfa.hash_recovery_code(c) for c in codes}

    actions = await _actions(org.tenant.id, member.user_id)
    assert "auth.mfa_enrollment_started" in actions
    assert "auth.mfa_enrollment_failed" in actions  # written although the 422 rolled back the request
    assert "auth.mfa_enrolled" in actions


async def test_confirm_without_start_is_409(member: Member) -> None:
    resp = await member.client.post(CONFIRM, json={"code": "123456"}, headers=member.h())
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "mfa_enrollment_not_started")


async def test_enrol_endpoints_require_csrf(member: Member) -> None:
    resp = await member.client.post(ENROLL)
    assert (resp.status_code, resp.json()["error"]["code"]) == (403, "csrf_failed")


# ---------------------------------------------------------------- sign-in with MFA
async def test_login_with_mfa_then_verify_opens_session(app: FastAPI, member: Member, clock: Clock) -> None:
    secret, _ = await _enroll(member, clock)
    clock.tick()
    async with _password_step(app, member) as client:
        assert (await client.get(ME)).status_code == 401  # no session yet
        token = client.cookies.get(MFA_COOKIE)
        assert token
        resp = await client.post(VERIFY, json={"code": _code(secret, clock)})
        assert resp.status_code == 200, resp.text
        assert resp.json()["user"]["id"] == str(member.user_id)
        assert USER_COOKIE in client.cookies
        assert MFA_COOKIE not in client.cookies
        assert (await client.get(ME)).status_code == 200
        # The challenge is single use.
        client.cookies.set(MFA_COOKIE, token)
        reuse = await client.post(VERIFY, json={"code": _code(secret, clock)})
        assert (reuse.status_code, reuse.json()["error"]["code"]) == (401, "mfa_challenge_expired")


async def test_code_replay_is_rejected(app: FastAPI, member: Member, clock: Clock) -> None:
    secret, _ = await _enroll(member, clock)
    clock.tick()
    code = _code(secret, clock)
    async with _password_step(app, member) as first:
        assert (await first.post(VERIFY, json={"code": code})).status_code == 200
    async with _password_step(app, member) as second:
        replay = await second.post(VERIFY, json={"code": code})
        assert (replay.status_code, replay.json()["error"]["code"]) == (401, "invalid_code")
        # The enrolment code (an older step) is refused too.
        clock.tick()
        assert (await second.post(VERIFY, json={"code": _code(secret, clock)})).status_code == 200


async def test_codes_within_one_step_are_accepted(app: FastAPI, member: Member, clock: Clock) -> None:
    secret, _ = await _enroll(member, clock)
    clock.tick(60)
    early = mfa.totp_code(secret, clock.step() - 1)
    async with _password_step(app, member) as client:
        assert (await client.post(VERIFY, json={"code": early})).status_code == 200
    stale = mfa.totp_code(secret, clock.step() - 3)
    clock.tick(60)
    async with _password_step(app, member) as client:
        resp = await client.post(VERIFY, json={"code": stale})
        assert resp.json()["error"]["code"] == "invalid_code"


async def test_recovery_code_signs_in_once(app: FastAPI, member: Member, clock: Clock, org: Org) -> None:
    _, codes = await _enroll(member, clock)
    async with _password_step(app, member) as client:
        resp = await client.post(VERIFY, json={"recovery_code": codes[0].lower().replace("-", " ")})
        assert resp.status_code == 200, resp.text
    async with _password_step(app, member) as client:
        reused = await client.post(VERIFY, json={"recovery_code": codes[0]})
        assert (reused.status_code, reused.json()["error"]["code"]) == (401, "invalid_code")
    async with tenant_session(org.tenant.id) as db:
        used = (
            await db.execute(
                select(MfaRecoveryCode).where(
                    MfaRecoveryCode.principal_id == member.user_id, MfaRecoveryCode.used_at.is_not(None)
                )
            )
        ).scalars()
        assert [r.code_hash for r in used] == [mfa.hash_recovery_code(codes[0])]
    actions = await _actions(org.tenant.id, member.user_id)
    assert "auth.mfa_recovery_code_used" in actions
    assert "auth.mfa_failed" in actions


async def test_verify_body_needs_exactly_one_factor(app: FastAPI, member: Member, clock: Clock) -> None:
    await _enroll(member, clock)
    async with _password_step(app, member) as client:
        both = await client.post(VERIFY, json={"code": "123456", "recovery_code": "AAAA-BBBB"})
        assert both.status_code == 422
        neither = await client.post(VERIFY, json={})
        assert neither.status_code == 422


async def test_five_wrong_codes_invalidate_the_challenge(
    app: FastAPI, member: Member, clock: Clock, org: Org
) -> None:
    secret, _ = await _enroll(member, clock)
    clock.tick()
    async with _password_step(app, member) as client:
        token = client.cookies.get(MFA_COOKIE)
        assert token
        for _ in range(4):
            resp = await client.post(VERIFY, json={"code": "000000"})
            assert (resp.status_code, resp.json()["error"]["code"]) == (401, "invalid_code")
        fifth = await client.post(VERIFY, json={"code": "000000"})
        assert (fifth.status_code, fifth.json()["error"]["code"]) == (401, "mfa_challenge_expired")
        assert MFA_COOKIE not in client.cookies
        client.cookies.set(MFA_COOKIE, token)  # even the right code no longer works on that challenge
        late = await client.post(VERIFY, json={"code": _code(secret, clock)})
        assert late.json()["error"]["code"] == "mfa_challenge_expired"
    async with platform_session() as db:
        challenge = (
            await db.execute(select(MfaChallenge).where(MfaChallenge.principal_id == member.user_id))
        ).scalar_one()
        assert (challenge.attempts, challenge.consumed_at is not None) == (5, True)
    assert (await _actions(org.tenant.id, member.user_id)).count("auth.mfa_failed") == 5
    # A new password step starts over.
    async with _password_step(app, member) as client:
        assert (await client.post(VERIFY, json={"code": _code(secret, clock)})).status_code == 200


async def test_expired_or_missing_challenge(app: FastAPI, member: Member, clock: Clock) -> None:
    secret, _ = await _enroll(member, clock)
    clock.tick()
    async with new_client(app) as anonymous:
        resp = await anonymous.post(VERIFY, json={"code": _code(secret, clock)})
        assert (resp.status_code, resp.json()["error"]["code"]) == (401, "mfa_challenge_expired")
    async with _password_step(app, member) as client:
        async with platform_session() as db:
            await db.execute(
                update(MfaChallenge)
                .where(MfaChallenge.principal_id == member.user_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        resp = await client.post(VERIFY, json={"code": _code(secret, clock)})
        assert (resp.status_code, resp.json()["error"]["code"]) == (401, "mfa_challenge_expired")


async def test_deactivated_user_cannot_finish_the_challenge(
    app: FastAPI, member: Member, clock: Clock, org: Org
) -> None:
    secret, _ = await _enroll(member, clock)
    clock.tick()
    async with _password_step(app, member) as client:
        async with tenant_session(org.tenant.id) as db:
            await db.execute(update(User).where(User.id == member.user_id).values(is_active=False))
        resp = await client.post(VERIFY, json={"code": _code(secret, clock)})
        assert resp.json()["error"]["code"] == "mfa_challenge_expired"


async def test_user_challenge_cannot_be_used_as_operator_challenge(
    app: FastAPI, member: Member, clock: Clock
) -> None:
    secret, _ = await _enroll(member, clock)
    clock.tick()
    async with _password_step(app, member) as client:
        resp = await client.post(f"{OP}/verify", json={"code": _code(secret, clock)})
        assert (resp.status_code, resp.json()["error"]["code"]) == (401, "mfa_challenge_expired")
        assert PLATFORM_COOKIE not in client.cookies


# ---------------------------------------------------------------- tenant requires MFA
async def test_tenant_requires_mfa_restricts_until_enrolment(
    app: FastAPI, org: Org, member: Member, clock: Clock
) -> None:
    assert (await org.admin.client.get(SECURITY)).json() == {"mfa_required": False}
    denied = await member.client.patch(SECURITY, json={"mfa_required": True}, headers=member.h())
    assert (denied.status_code, denied.json()["error"]["details"]["permission"]) == (403, "settings:manage")
    resp = await org.admin.client.patch(SECURITY, json={"mfa_required": True}, headers=org.admin.h())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"mfa_required": True}
    # The admin has no MFA either: restricted too, only auth/me, logout and auth/mfa/* remain usable.
    admin_blocked = await org.admin.client.get(SECURITY)
    assert admin_blocked.json()["error"]["code"] == "mfa_enrollment_required"

    # Existing sessions of users without MFA are restricted from the next request on.
    blocked = await member.client.get(TEAMS)
    assert (blocked.status_code, blocked.json()["error"]["code"]) == (403, "mfa_enrollment_required")

    async with new_client(app) as client:
        login = await client.post(
            LOGIN, json={"tenant_slug": member.slug, "email": member.email, "password": USER_PASSWORD}
        )
        assert login.status_code == 200, login.text
        assert login.json()["mfa_enrollment_required"] is True
        h = csrf(client)
        me = await client.get(ME)
        assert (me.status_code, me.json()["mfa_enrollment_required"]) == (200, True)
        blocked = await client.get(TEAMS)
        assert blocked.json()["error"]["code"] == "mfa_enrollment_required"
        start = await client.post(ENROLL, headers=h)
        assert start.status_code == 200
        secret = start.json()["secret"]
        confirm = await client.post(CONFIRM, json={"code": _code(secret, clock)}, headers=h)
        assert confirm.status_code == 200
        assert (await client.get(TEAMS)).status_code == 200
        assert (await client.get(ME)).json()["mfa_enrollment_required"] is False
        # Cannot switch MFA off while the tenant requires it.
        clock.tick()
        off = await client.post(DISABLE, json={"code": _code(secret, clock)}, headers=h)
        assert (off.status_code, off.json()["error"]["code"]) == (409, "mfa_required_by_tenant")
        assert (await client.post(LOGOUT, headers=h)).status_code == 204

    # Subsequent sign-ins ask for a code.
    async with _password_step(app, member) as client:
        clock.tick()
        assert (await client.post(VERIFY, json={"code": _code(secret, clock)})).status_code == 200

    async with tenant_session(org.tenant.id) as db:
        rows = await db.execute(
            select(audit.AuditLog.data).where(audit.AuditLog.action == "tenant.security_updated")
        )
        assert list(rows.scalars()) == [{"mfa_required": {"old": False, "new": True}}]


async def test_restricted_session_can_still_sign_out(org: Org, member: Member) -> None:
    await org.admin.client.patch(SECURITY, json={"mfa_required": True}, headers=org.admin.h())
    assert (await member.client.get(ME)).status_code == 200
    assert (await member.client.post(LOGOUT, headers=member.h())).status_code == 204


# ---------------------------------------------------------------- disable / regenerate
async def test_disable_requires_a_current_code(member: Member, clock: Clock, org: Org) -> None:
    not_enabled = await member.client.post(DISABLE, json={"code": "123456"}, headers=member.h())
    assert (not_enabled.status_code, not_enabled.json()["error"]["code"]) == (409, "mfa_not_enabled")
    secret, _ = await _enroll(member, clock)
    clock.tick()
    wrong = await member.client.post(DISABLE, json={"code": "000000"}, headers=member.h())
    assert (wrong.status_code, wrong.json()["error"]["code"]) == (422, "invalid_code")
    ok = await member.client.post(DISABLE, json={"code": _code(secret, clock)}, headers=member.h())
    assert ok.status_code == 204, ok.text
    assert (await member.client.get(ME)).json()["user"]["mfa_enabled"] is False
    async with tenant_session(org.tenant.id) as db:
        user = await db.get(User, member.user_id)
        assert user is not None
        assert (user.mfa_secret_encrypted, user.mfa_last_step) == (None, None)
        left = (
            await db.execute(select(MfaRecoveryCode.id).where(MfaRecoveryCode.principal_id == member.user_id))
        ).all()
        assert left == []
    actions = await _actions(org.tenant.id, member.user_id)
    assert {"auth.mfa_disable_failed", "auth.mfa_disabled"} <= set(actions)


async def test_regenerate_recovery_codes_replaces_old_ones(
    app: FastAPI, member: Member, clock: Clock
) -> None:
    secret, old = await _enroll(member, clock)
    clock.tick()
    wrong = await member.client.post(REGENERATE, json={"code": "000000"}, headers=member.h())
    assert wrong.status_code == 422
    resp = await member.client.post(REGENERATE, json={"code": _code(secret, clock)}, headers=member.h())
    assert resp.status_code == 200, resp.text
    new = resp.json()["recovery_codes"]
    assert len(new) == 10 and not set(new) & set(old)
    async with _password_step(app, member) as client:
        stale = await client.post(VERIFY, json={"recovery_code": old[0]})
        assert stale.json()["error"]["code"] == "invalid_code"
        assert (await client.post(VERIFY, json={"recovery_code": new[0]})).status_code == 200


# ---------------------------------------------------------------- platform operators
async def test_operator_mfa_flow(
    app: FastAPI, op_client: httpx.AsyncClient, operator: OperatorCreds, clock: Clock
) -> None:
    h = csrf(op_client)
    start = await op_client.post(f"{OP}/enroll", headers=h)
    assert start.status_code == 200, start.text
    secret = start.json()["secret"]
    confirm = await op_client.post(f"{OP}/enroll/confirm", json={"code": _code(secret, clock)}, headers=h)
    assert confirm.status_code == 200, confirm.text
    codes = confirm.json()["recovery_codes"]

    async with new_client(app) as client:
        login = await client.post(OP_LOGIN, json={"email": operator.email, "password": operator.password})
        assert (login.status_code, login.json()) == (200, {"mfa_required": True})
        assert PLATFORM_COOKIE not in client.cookies
        replay = await client.post(f"{OP}/verify", json={"code": _code(secret, clock)})
        assert replay.json()["error"]["code"] == "invalid_code"
        clock.tick()
        ok = await client.post(f"{OP}/verify", json={"code": _code(secret, clock)})
        assert ok.status_code == 200, ok.text
        assert ok.json()["mfa_enabled"] is True
        assert PLATFORM_COOKIE in client.cookies

    async with new_client(app) as client:
        await client.post(OP_LOGIN, json={"email": operator.email, "password": operator.password})
        assert (await client.post(f"{OP}/verify", json={"recovery_code": codes[1]})).status_code == 200

    # Operator codes are platform rows: invisible to any tenant session.
    async with tenant_session(uuid.uuid4()) as db:
        assert (await db.execute(select(MfaRecoveryCode.id))).all() == []
    async with platform_session() as db:
        rows = (
            await db.execute(
                select(MfaRecoveryCode.tenant_id).where(MfaRecoveryCode.principal_id == operator.id)
            )
        ).scalars()
        assert set(rows) == {None}
        op = await db.get(PlatformOperator, operator.id)
        assert op is not None and op.mfa_enabled

    clock.tick()
    off = await op_client.post(f"{OP}/disable", json={"code": _code(secret, clock)}, headers=h)
    assert off.status_code == 204, off.text


async def test_tenant_session_cannot_read_or_forge_operator_codes(org: Org) -> None:
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="row-level security|check constraint"):
        async with tenant_session(org.tenant.id) as db:
            db.add(
                MfaRecoveryCode(
                    tenant_id=None, principal_type="platform", principal_id=uuid.uuid4(), code_hash="x" * 64
                )
            )


def test_totp_matching_window_and_replay_guard() -> None:
    secret = mfa.new_secret()
    now = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)
    step = mfa.current_step(now)
    for offset in (-1, 0, 1):
        assert (
            mfa.matching_step(secret, mfa.totp_code(secret, step + offset), last_step=None, now=now)
            == step + offset
        )
    assert mfa.matching_step(secret, mfa.totp_code(secret, step + 2), last_step=None, now=now) is None
    assert mfa.matching_step(secret, mfa.totp_code(secret, step), last_step=step, now=now) is None


def test_recovery_code_normalization() -> None:
    code = mfa.new_recovery_code()
    assert RECOVERY_RE.fullmatch(code)
    assert mfa.hash_recovery_code(code) == mfa.hash_recovery_code(" " + code.lower().replace("-", " ") + " ")
