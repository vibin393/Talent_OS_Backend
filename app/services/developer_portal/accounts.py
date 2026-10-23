# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Developer accounts (Epic 74, story 74.1, spec 0.2): sign-up (no enumeration), email verification, sign-in with the
P1 lockout, mandatory TOTP MFA (enrolment forced on the first sign-in, replay refused through ``mfa_last_step``,
hashed recovery codes), password reset (revokes every session) and the ``tos_developer`` sessions
(``tos_dev_<token>``; only the SHA-256 is stored; 12 h idle / 7 d absolute).

A password sign-in opens a *pending* session (``mfa_passed = false``, 5 minutes, cookie ``tos_dev_mfa``); completing
MFA (verify or enrolment) replaces it with a full session (cookie ``tos_developer``).
"""

import dataclasses
import secrets
import uuid
from datetime import timedelta

import pyotp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import platform_session
from app.core.errors import ConflictError, Forbidden, Gone, Unauthorized, ValidationFailed
from app.core.security import decrypt_str, encrypt_str, hash_password, hash_token, verify_password
from app.core.settings import get_settings
from app.models.developer import (
    DeveloperAccount,
    DeveloperEmailToken,
    DeveloperRecoveryCode,
    DeveloperSession,
    PartnerOrgMember,
)
from app.models.oauth import PartnerOrg
from app.schemas.developer import (
    LoginIn,
    MeOut,
    MfaEnrollOut,
    MfaVerifyIn,
    OrgOut,
    RecoveryCodesOut,
    SignupIn,
)
from app.services import auth_service, mfa_service
from app.services.developer_portal import audit_log, notifier
from app.services.developer_portal.common import now

SESSION_PREFIX = "tos_dev_"
PENDING_MINUTES = 5
RESET_HOURS = 1
RECOVERY_CODES = 10
ISSUER = "Talent OS for Developers"


@dataclasses.dataclass(frozen=True, slots=True)
class IssuedDevSession:
    token: str
    session_id: uuid.UUID
    max_age: int


def _new_session_token() -> str:
    return SESSION_PREFIX + secrets.token_urlsafe(32)


async def _open_session(db: AsyncSession, account: DeveloperAccount, *, mfa_passed: bool, ip: str | None, user_agent: str | None) -> IssuedDevSession:
    s = get_settings()
    at = now()
    token = _new_session_token()
    if mfa_passed:
        idle, absolute = timedelta(hours=s.developer_session_idle_hours), timedelta(days=s.developer_session_absolute_days)
    else:
        idle = absolute = timedelta(minutes=PENDING_MINUTES)
    row = DeveloperSession(
        id=uuid.uuid4(), account_id=account.id, token_hash=hash_token(token), mfa_passed=mfa_passed, created_at=at,
        last_seen_at=at, idle_expires_at=at + idle, absolute_expires_at=at + absolute, ip=ip,
        user_agent=(user_agent or "")[:200] or None,
    )  # fmt: skip
    db.add(row)
    await db.flush()
    return IssuedDevSession(token=token, session_id=row.id, max_age=int(absolute.total_seconds()))


async def find_session(db: AsyncSession, token: str | None, *, pending: bool) -> tuple[DeveloperSession, DeveloperAccount] | None:
    """The live session for ``token`` (pending = the MFA step session) and its active account."""
    if not token or not token.startswith(SESSION_PREFIX):
        return None
    row = (await db.execute(select(DeveloperSession).where(DeveloperSession.token_hash == hash_token(token)))).scalar_one_or_none()
    at = now()
    if row is None or row.revoked_at is not None or at >= row.idle_expires_at or at >= row.absolute_expires_at:
        return None
    if row.mfa_passed == pending:
        return None
    account = await db.get(DeveloperAccount, row.account_id)
    if account is None or account.status != "active":
        return None
    if not pending:
        idle = timedelta(hours=get_settings().developer_session_idle_hours)
        if at - row.last_seen_at >= timedelta(minutes=1):
            row.last_seen_at = at
            row.idle_expires_at = min(at + idle, row.absolute_expires_at)
    return row, account


async def orgs_of(db: AsyncSession, account_id: uuid.UUID) -> list[OrgOut]:
    rows = await db.execute(
        select(PartnerOrg, PartnerOrgMember.role)
        .join(PartnerOrgMember, PartnerOrgMember.partner_org_id == PartnerOrg.id)
        .where(PartnerOrgMember.account_id == account_id, PartnerOrgMember.removed_at.is_(None))
        .order_by(PartnerOrg.name)
    )
    return [org_out(o, role) for o, role in rows.all()]


def org_out(o: PartnerOrg, role: str | None = None) -> OrgOut:
    return OrgOut(
        id=o.id, name=o.name, slug=str(o.slug), status=o.status, verification_status=o.verification_status,
        verified_at=o.verified_at, verification_expires_at=o.verification_expires_at, role=role,  # type: ignore[arg-type]
    )  # fmt: skip


async def me(account_id: uuid.UUID) -> MeOut:
    async with platform_session() as db:
        account = await db.get(DeveloperAccount, account_id)
        if account is None:
            raise Unauthorized()
        return MeOut(id=account.id, email=str(account.email), name=account.name, mfa_enabled=account.mfa_enabled_at is not None,
                     orgs=await orgs_of(db, account.id))  # fmt: skip


async def _email_token(db: AsyncSession, account_id: uuid.UUID, purpose: str, ttl: timedelta) -> str:
    token = secrets.token_urlsafe(32)
    db.add(DeveloperEmailToken(id=uuid.uuid4(), account_id=account_id, purpose=purpose, token_hash=hash_token(token), expires_at=now() + ttl))
    await db.flush()
    return token


async def _consume_email_token(db: AsyncSession, token: str, purpose: str) -> DeveloperEmailToken:
    row = (
        await db.execute(
            select(DeveloperEmailToken).where(DeveloperEmailToken.token_hash == hash_token(token)).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.purpose != purpose or row.used_at is not None or row.expires_at <= now():
        raise Gone("This link is invalid or has expired", code="link_expired")
    row.used_at = now()
    return row


# ================================================================ sign-up / verification
async def signup(data: SignupIn, *, ip: str | None = None) -> None:
    """Always succeeds outwardly (202): an existing address gets an "account exists" email instead (no enumeration)."""
    s = get_settings()
    if not s.developer_portal_enabled:
        raise Forbidden("Developer sign-ups are paused", code="signups_paused")
    if data.accept_terms_version != s.developer_terms_version:
        raise ValidationFailed("Accept the current Developer Terms", code="terms_required", details={"fields": {"accept_terms_version": "Accept the current terms"}})
    auth_service.validate_password_strength(data.password)
    email = str(data.email).strip().lower()
    async with platform_session() as db:
        existing = (await db.execute(select(DeveloperAccount).where(DeveloperAccount.email == email))).scalar_one_or_none()
        if existing is not None:
            if existing.email_verified_at is None and existing.status == "active":
                token = await _email_token(db, existing.id, "verify_email", timedelta(hours=s.developer_email_verify_hours))
                await db.commit()
                await notifier.verify_email(email, token)
            else:
                await notifier.account_exists(email)
            return
        account = DeveloperAccount(
            id=uuid.uuid4(), email=email, name=data.name.strip()[:120], password_hash=hash_password(data.password),
            status="active", terms_version=data.accept_terms_version,
        )  # fmt: skip
        db.add(account)
        await db.flush()
        token = await _email_token(db, account.id, "verify_email", timedelta(hours=s.developer_email_verify_hours))
        audit_log.add(db, "account.created", account_id=account.id, ip_hash=hash_token(ip or "")[:12])
    await notifier.verify_email(email, token)


async def verify_email(token: str) -> None:
    async with platform_session() as db:
        row = await _consume_email_token(db, token, "verify_email")
        account = await db.get(DeveloperAccount, row.account_id)
        if account is not None and account.email_verified_at is None:
            account.email_verified_at = now()
            audit_log.add(db, "account.email_verified", account_id=account.id)


# ================================================================ sign-in + MFA
async def login(data: LoginIn, *, ip: str | None, user_agent: str | None) -> tuple[str, IssuedDevSession]:
    """Password step: returns ``("mfa_required" | "enroll_required", pending session)``. Unverified emails cannot sign in."""
    email = str(data.email).strip().lower()
    async with platform_session() as db:
        account = (
            await db.execute(select(DeveloperAccount).where(DeveloperAccount.email == email).with_for_update())
        ).scalar_one_or_none()
        check = auth_service.check_login_attempt(
            account, password_hash=account.password_hash if account else None,
            is_active=account is not None and account.status == "active", password=data.password, now=now(),
        )  # fmt: skip
        if check.outcome != "ok" or account is None:
            if account is not None:
                audit_log.add(db, "account.sign_in_failed", account_id=account.id, outcome=check.outcome)
            await db.commit()
            raise check.error()
        if account.email_verified_at is None:
            await db.commit()
            raise Forbidden("Verify your email address first", code="email_not_verified")
        issued = await _open_session(db, account, mfa_passed=False, ip=ip, user_agent=user_agent)
        step = "mfa_required" if account.mfa_enabled_at is not None else "enroll_required"
    return step, issued


async def mfa_enroll_start(pending_token: str | None) -> MfaEnrollOut:
    async with platform_session() as db:
        found = await find_session(db, pending_token, pending=True)
        if found is None:
            raise Unauthorized("Sign in again", code="mfa_challenge_expired")
        _, account = found
        if account.mfa_enabled_at is not None:
            raise ConflictError("Two-step verification is already on", code="mfa_already_enabled")
        secret = mfa_service.new_secret()
        account.mfa_secret_enc = encrypt_str("mfa", secret)
        account.mfa_last_step = None
        uri = pyotp.TOTP(secret).provisioning_uri(name=str(account.email), issuer_name=ISSUER)
        display = " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))
        return MfaEnrollOut(otpauth_uri=uri, secret_display=display)


async def _new_recovery_codes(db: AsyncSession, account_id: uuid.UUID) -> list[str]:
    for old in (await db.execute(select(DeveloperRecoveryCode).where(DeveloperRecoveryCode.account_id == account_id))).scalars():
        await db.delete(old)
    await db.flush()
    codes = [mfa_service.new_recovery_code() for _ in range(RECOVERY_CODES)]
    for c in codes:
        db.add(DeveloperRecoveryCode(id=uuid.uuid4(), account_id=account_id, code_hash=mfa_service.hash_recovery_code(c)))
    await db.flush()
    return codes


def _totp_ok(account: DeveloperAccount, code: str) -> bool:
    if not account.mfa_secret_enc:
        return False
    step = mfa_service.matching_step(decrypt_str("mfa", account.mfa_secret_enc), code.strip(), last_step=account.mfa_last_step, now=now())
    if step is None:
        return False
    account.mfa_last_step = step
    return True


async def _upgrade(db: AsyncSession, pending: DeveloperSession, account: DeveloperAccount, *, ip: str | None, user_agent: str | None) -> IssuedDevSession:
    pending.revoked_at = now()
    issued = await _open_session(db, account, mfa_passed=True, ip=ip, user_agent=user_agent)
    known = (
        await db.execute(
            select(DeveloperSession.id).where(DeveloperSession.account_id == account.id, DeveloperSession.mfa_passed.is_(True),
                                              DeveloperSession.ip == ip, DeveloperSession.id != issued.session_id).limit(1)
        )
    ).first()  # fmt: skip
    first_login = account.last_login_at is None
    account.last_login_at = now()
    audit_log.add(db, "account.signed_in", account_id=account.id)
    if known is None and not first_login:
        await db.flush()
        email = str(account.email)
        await notifier.new_sign_in(email, ip)
    return issued


async def mfa_enroll_confirm(pending_token: str | None, code: str, *, ip: str | None, user_agent: str | None) -> tuple[RecoveryCodesOut, IssuedDevSession, uuid.UUID]:
    async with platform_session() as db:
        found = await find_session(db, pending_token, pending=True)
        if found is None:
            raise Unauthorized("Sign in again", code="mfa_challenge_expired")
        pending, account = found
        if account.mfa_enabled_at is not None:
            raise ConflictError("Two-step verification is already on", code="mfa_already_enabled")
        if not _totp_ok(account, code):
            raise ValidationFailed("That code didn't work. Try the newest code from your app.", code="invalid_code")
        account.mfa_enabled_at = now()
        codes = await _new_recovery_codes(db, account.id)
        audit_log.add(db, "account.mfa_enrolled", account_id=account.id)
        issued = await _upgrade(db, pending, account, ip=ip, user_agent=user_agent)
        return RecoveryCodesOut(recovery_codes=codes), issued, account.id


async def mfa_verify(pending_token: str | None, data: MfaVerifyIn, *, ip: str | None, user_agent: str | None) -> tuple[IssuedDevSession, uuid.UUID]:
    async with platform_session() as db:
        found = await find_session(db, pending_token, pending=True)
        if found is None:
            raise Unauthorized("Sign in again", code="mfa_challenge_expired")
        pending, account = found
        if account.mfa_enabled_at is None:
            raise Forbidden("Set up two-step verification first", code="mfa_enrollment_required")
        ok = False
        if data.code:
            ok = _totp_ok(account, data.code)
        elif data.recovery_code:
            row = (
                await db.execute(
                    select(DeveloperRecoveryCode).where(
                        DeveloperRecoveryCode.account_id == account.id,
                        DeveloperRecoveryCode.code_hash == mfa_service.hash_recovery_code(data.recovery_code),
                        DeveloperRecoveryCode.used_at.is_(None),
                    ).with_for_update()
                )
            ).scalar_one_or_none()
            if row is not None:
                row.used_at = now()
                ok = True
        if not ok:
            audit_log.add(db, "account.sign_in_failed", account_id=account.id, outcome="mfa")
            await db.commit()
            raise Unauthorized("That code didn't work", code="invalid_code")
        issued = await _upgrade(db, pending, account, ip=ip, user_agent=user_agent)
        return issued, account.id


async def logout(account_id: uuid.UUID, session_id: uuid.UUID | None, *, everywhere: bool = False) -> None:
    async with platform_session() as db:
        stmt = update(DeveloperSession).where(DeveloperSession.account_id == account_id, DeveloperSession.revoked_at.is_(None))
        if not everywhere:
            stmt = stmt.where(DeveloperSession.id == session_id)
        await db.execute(stmt.values(revoked_at=now()))


# ================================================================ password reset / close
async def request_password_reset(email: str) -> None:
    address = email.strip().lower()
    async with platform_session() as db:
        account = (await db.execute(select(DeveloperAccount).where(DeveloperAccount.email == address))).scalar_one_or_none()
        if account is None or account.status != "active":
            return
        token = await _email_token(db, account.id, "reset_password", timedelta(hours=RESET_HOURS))
    await notifier.password_reset(address, token)


async def reset_password(token: str, new_password: str) -> None:
    auth_service.validate_password_strength(new_password)
    async with platform_session() as db:
        row = await _consume_email_token(db, token, "reset_password")
        account = await db.get(DeveloperAccount, row.account_id)
        if account is None:
            raise Gone("This link is invalid or has expired", code="link_expired")
        account.password_hash = hash_password(new_password)
        auth_service.reset_login_throttle(account)
        await db.execute(
            update(DeveloperSession).where(DeveloperSession.account_id == account.id, DeveloperSession.revoked_at.is_(None)).values(revoked_at=now())
        )
        audit_log.add(db, "account.password_changed", account_id=account.id)


async def close_account(account_id: uuid.UUID, *, password: str) -> None:
    from app.services.developer_portal import erasure

    async with platform_session() as db:
        account = await db.get(DeveloperAccount, account_id)
        if account is None or not verify_password(account.password_hash, password):
            raise Forbidden("Enter your current password", code="password_incorrect")
        owned = list(
            (await db.execute(select(PartnerOrgMember.partner_org_id).where(
                PartnerOrgMember.account_id == account_id, PartnerOrgMember.role == "owner", PartnerOrgMember.removed_at.is_(None)))).scalars()
        )  # fmt: skip
        for org_id in owned:
            others = (await db.execute(select(PartnerOrgMember.id).where(
                PartnerOrgMember.partner_org_id == org_id, PartnerOrgMember.role == "owner", PartnerOrgMember.removed_at.is_(None),
                PartnerOrgMember.account_id != account_id).limit(1))).first()  # fmt: skip
            if others is None:
                raise ConflictError("Make someone else an owner of your organisation first", code="last_owner")
    await erasure.erase_account(account_id)


async def purge_unverified() -> int:
    """Daily: delete accounts whose email was never verified within ``developer_unverified_purge_days``."""
    from sqlalchemy import delete

    cutoff = now() - timedelta(days=get_settings().developer_unverified_purge_days)
    async with platform_session() as db:
        result = await db.execute(
            delete(DeveloperAccount).where(DeveloperAccount.email_verified_at.is_(None), DeveloperAccount.created_at < cutoff)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


__all__ = [
    "SESSION_PREFIX",
    "IssuedDevSession",
    "close_account",
    "find_session",
    "login",
    "logout",
    "me",
    "mfa_enroll_confirm",
    "mfa_enroll_start",
    "mfa_verify",
    "org_out",
    "orgs_of",
    "purge_unverified",
    "request_password_reset",
    "reset_password",
    "signup",
    "verify_email",
]
