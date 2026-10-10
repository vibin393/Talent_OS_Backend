"""TOTP multi-factor authentication (story 1.4) for tenant users and platform operators.

* TOTP per RFC 6238: 30 s steps, 6 digits, accepted within +-1 step. The base32 secret is stored encrypted
  (Fernet, ``MFA_ENCRYPTION_KEY``) in ``mfa_secret_encrypted``; it is *pending* until the first valid code
  confirms enrolment. ``mfa_last_step`` makes every code single-use (a step <= the last used one is refused).
* Sign-in: the password step (``service.login_user`` / ``login_operator``) issues a 5-minute single-use
  challenge behind the httpOnly ``tos_mfa`` cookie instead of a session; ``verify_*_challenge`` accepts a
  TOTP code or an unused recovery code and opens the session. 5 wrong codes invalidate the challenge.
* 10 recovery codes (80 bits each, shown once, SHA-256 stored) are issued on enrolment and on regeneration.
* Tenants may require MFA (``tenants.mfa_required``): users without MFA get restricted sessions (see
  ``service.authenticate_user``) until they enrol.
* Enrolment, disable, verify failures and recovery-code use are audited. Failure events of request-scoped
  operations are written in their own transaction so the error response does not roll them back.
"""

import dataclasses
import hmac
import re
import secrets
import uuid
from datetime import UTC, datetime

import pyotp
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import ConflictError, NotFound, Unauthorized, ValidationFailed
from app.core.security import MFA_COOKIE, PrincipalType, clear_cookie, decrypt_str, encrypt_str, hash_token
from app.models.identity import MfaChallenge, MfaRecoveryCode, PlatformOperator, Tenant, User
from app.models.identity import Session as AuthSession
from app.schemas.identity import (
    MfaEnrollOut,
    MfaVerifyIn,
    RecoveryCodesOut,
    TenantSecurityOut,
    TenantSecurityUpdate,
)
from app.services import auth_service as service

TOTP_INTERVAL = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1  # accepted steps: current -1 .. current +1
MAX_CHALLENGE_ATTEMPTS = 5
RECOVERY_CODE_COUNT = 10
ISSUER = "Talent OS"
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 symbols, no 0/O/1/I
_RECOVERY_GROUPS = 4
_RECOVERY_GROUP_LEN = 4  # 16 symbols x 5 bits = 80 bits

MfaAccount = User | PlatformOperator


def _now() -> datetime:
    return datetime.now(UTC)


# ================================================================ primitives
def new_secret() -> str:
    return pyotp.random_base32()  # 32 base32 chars = 160 bits


def totp_code(secret: str, step: int) -> str:
    return str(pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL).generate_otp(step))


def current_step(now: datetime) -> int:
    return int(now.timestamp()) // TOTP_INTERVAL


def matching_step(secret: str, code: str, *, last_step: int | None, now: datetime) -> int | None:
    """Return the step (within +-``TOTP_WINDOW``) whose code is ``code`` and is newer than ``last_step``."""
    step_now = current_step(now)
    for step in range(step_now - TOTP_WINDOW, step_now + TOTP_WINDOW + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(totp_code(secret, step).encode(), code.encode()):
            return step
    return None


def new_recovery_code() -> str:
    return "-".join(
        "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_GROUP_LEN))
        for _ in range(_RECOVERY_GROUPS)
    )


def normalize_recovery_code(code: str) -> str:
    return re.sub(r"[\s-]", "", code).upper()


def hash_recovery_code(code: str) -> str:
    return hash_token(normalize_recovery_code(code))


def _secret_of(account: MfaAccount) -> str | None:
    return decrypt_str("mfa", account.mfa_secret_encrypted) if account.mfa_secret_encrypted else None


# ================================================================ errors
def invalid_code() -> Unauthorized:
    return Unauthorized("The code is invalid or was already used", code="invalid_code")


def challenge_expired() -> Unauthorized:
    exc = Unauthorized("The sign-in attempt expired; enter your password again", code="mfa_challenge_expired")
    exc.response_hooks.append(lambda response: clear_cookie(response, MFA_COOKIE))
    return exc


def invalid_code_422() -> ValidationFailed:
    message = "The code is invalid or was already used"
    return ValidationFailed(
        message,
        code="invalid_code",
        details={"fields": [{"field": "code", "message": message, "type": "invalid_code"}]},
    )


def _already_enabled() -> ConflictError:
    return ConflictError("Multi-factor authentication is already enabled", code="mfa_already_enabled")


def _not_enabled() -> ConflictError:
    return ConflictError("Multi-factor authentication is not enabled", code="mfa_not_enabled")


# ================================================================ generic account operations
@dataclasses.dataclass(frozen=True, slots=True)
class Subject:
    """The MFA account's principal: audit entity + recovery-code owner."""

    principal_type: PrincipalType
    principal_id: uuid.UUID
    tenant_id: uuid.UUID | None

    @property
    def entity_type(self) -> str:
        return "user" if self.principal_type == "user" else "platform_operator"


def _subject(account: MfaAccount) -> Subject:
    if isinstance(account, User):
        return Subject("user", account.id, account.tenant_id)
    return Subject("platform", account.id, None)


def _record(db: AsyncSession, subject: Subject, action: str, **data: object) -> None:
    audit.record(
        action,
        subject.entity_type,
        subject.principal_id,
        session=db,
        actor_type=subject.principal_type,
        actor_id=subject.principal_id,
        **data,
    )


async def _record_detached(subject: Subject, action: str, **data: object) -> None:
    """Audit a failure in its own transaction (the request's transaction is rolled back by the error)."""
    cm = tenant_session(subject.tenant_id) if subject.tenant_id is not None else platform_session()
    async with cm as db:
        _record(db, subject, action, **data)


async def _replace_recovery_codes(db: AsyncSession, subject: Subject) -> list[str]:
    await db.execute(
        delete(MfaRecoveryCode).where(
            MfaRecoveryCode.principal_type == subject.principal_type,
            MfaRecoveryCode.principal_id == subject.principal_id,
        )
    )
    codes = [new_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    db.add_all(
        MfaRecoveryCode(
            tenant_id=subject.tenant_id,
            principal_type=subject.principal_type,
            principal_id=subject.principal_id,
            code_hash=hash_recovery_code(code),
        )
        for code in codes
    )
    await db.flush()
    return codes


async def _use_recovery_code(db: AsyncSession, subject: Subject, code: str, now: datetime) -> bool:
    """Atomically mark an unused recovery code used; False if unknown or already used."""
    result = await db.execute(
        update(MfaRecoveryCode)
        .where(
            MfaRecoveryCode.principal_type == subject.principal_type,
            MfaRecoveryCode.principal_id == subject.principal_id,
            MfaRecoveryCode.code_hash == hash_recovery_code(code),
            MfaRecoveryCode.used_at.is_(None),
        )
        .values(used_at=now)
        .returning(MfaRecoveryCode.id)
    )
    return result.first() is not None


def _check_totp(account: MfaAccount, code: str, now: datetime) -> bool:
    """Verify ``code`` against the account's secret; on success consume its time step (no replay)."""
    secret = _secret_of(account)
    if secret is None:
        return False
    step = matching_step(secret, code, last_step=account.mfa_last_step, now=now)
    if step is None:
        return False
    account.mfa_last_step = step
    return True


async def _start_enrollment(db: AsyncSession, account: MfaAccount, label: str) -> MfaEnrollOut:
    if account.mfa_enabled:
        raise _already_enabled()
    secret = new_secret()
    account.mfa_secret_encrypted = encrypt_str("mfa", secret)
    account.mfa_last_step = None
    _record(db, _subject(account), "auth.mfa_enrollment_started")
    await db.flush()
    uri = pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL).provisioning_uri(
        name=label, issuer_name=ISSUER
    )
    return MfaEnrollOut(secret=secret, otpauth_uri=uri)


async def _confirm_enrollment(db: AsyncSession, account: MfaAccount, code: str) -> RecoveryCodesOut:
    subject = _subject(account)
    if account.mfa_enabled:
        raise _already_enabled()
    if account.mfa_secret_encrypted is None:
        raise ConflictError("Start the enrolment first", code="mfa_enrollment_not_started")
    if not _check_totp(account, code, _now()):
        await _record_detached(subject, "auth.mfa_enrollment_failed", reason="invalid_code")
        raise invalid_code_422()
    account.mfa_enabled = True
    codes = await _replace_recovery_codes(db, subject)
    if subject.principal_type == "user":
        await db.execute(
            update(AuthSession)
            .where(
                AuthSession.principal_type == "user",
                AuthSession.principal_id == account.id,
                AuthSession.mfa_enrollment_required.is_(True),
            )
            .values(mfa_enrollment_required=False)
        )
    _record(db, subject, "auth.mfa_enrolled")
    await db.flush()
    return RecoveryCodesOut(recovery_codes=codes)


async def _require_current_code(account: MfaAccount, code: str, failure_action: str) -> None:
    if not account.mfa_enabled:
        raise _not_enabled()
    if not _check_totp(account, code, _now()):
        await _record_detached(_subject(account), failure_action, reason="invalid_code")
        raise invalid_code_422()


async def _disable(db: AsyncSession, account: MfaAccount, code: str) -> None:
    await _require_current_code(account, code, "auth.mfa_disable_failed")
    subject = _subject(account)
    account.mfa_enabled = False
    account.mfa_secret_encrypted = None
    account.mfa_last_step = None
    await db.execute(
        delete(MfaRecoveryCode).where(
            MfaRecoveryCode.principal_type == subject.principal_type,
            MfaRecoveryCode.principal_id == subject.principal_id,
        )
    )
    _record(db, subject, "auth.mfa_disabled")
    await db.flush()


async def _regenerate(db: AsyncSession, account: MfaAccount, code: str) -> RecoveryCodesOut:
    await _require_current_code(account, code, "auth.mfa_recovery_codes_regenerate_failed")
    subject = _subject(account)
    codes = await _replace_recovery_codes(db, subject)
    _record(db, subject, "auth.mfa_recovery_codes_regenerated")
    await db.flush()
    return RecoveryCodesOut(recovery_codes=codes)


async def _second_factor(
    db: AsyncSession, account: MfaAccount, body: MfaVerifyIn, now: datetime
) -> str | None:
    """Return the method that verified (``totp`` / ``recovery_code``), or None."""
    if body.code is not None:
        return "totp" if _check_totp(account, body.code, now) else None
    if body.recovery_code is not None and await _use_recovery_code(
        db, _subject(account), body.recovery_code, now
    ):
        _record(db, _subject(account), "auth.mfa_recovery_code_used")
        return "recovery_code"
    return None


def _challenge_usable(challenge: MfaChallenge | None, now: datetime) -> bool:
    return (
        challenge is not None
        and challenge.consumed_at is None
        and challenge.expires_at > now
        and challenge.attempts < MAX_CHALLENGE_ATTEMPTS
    )


def _count_failure(
    db: AsyncSession, challenge: MfaChallenge, subject: Subject, now: datetime
) -> Unauthorized:
    """Count a wrong code on the challenge (committed by the caller before raising)."""
    challenge.attempts += 1
    exhausted = challenge.attempts >= MAX_CHALLENGE_ATTEMPTS
    if exhausted:
        challenge.consumed_at = now
    _record(db, subject, "auth.mfa_failed", reason="too_many_attempts" if exhausted else "invalid_code")
    return challenge_expired() if exhausted else invalid_code()


async def _find_challenge(token: str | None, principal_type: PrincipalType) -> MfaChallenge | None:
    if not token:
        return None
    async with platform_session() as db:
        return (
            await db.execute(
                select(MfaChallenge).where(
                    MfaChallenge.token_hash == hash_token(token),
                    MfaChallenge.principal_type == principal_type,
                )
            )
        ).scalar_one_or_none()


# ================================================================ tenant users
def _user(ctx: RequestContext) -> uuid.UUID:
    return service.require_user(ctx).id


async def _load_user(ctx: RequestContext, db: AsyncSession) -> User:
    user = await db.get(User, _user(ctx), with_for_update=True)
    if user is None:  # pragma: no cover - authenticated principal always exists
        raise Unauthorized()
    return user


async def enroll_user(ctx: RequestContext, db: AsyncSession) -> MfaEnrollOut:
    user = await _load_user(ctx, db)
    return await _start_enrollment(db, user, user.email)


async def confirm_user_enrollment(ctx: RequestContext, db: AsyncSession, code: str) -> RecoveryCodesOut:
    return await _confirm_enrollment(db, await _load_user(ctx, db), code)


async def disable_user_mfa(ctx: RequestContext, db: AsyncSession, code: str) -> None:
    user = await _load_user(ctx, db)
    tenant = await db.get(Tenant, user.tenant_id)
    if user.mfa_enabled and tenant is not None and tenant.mfa_required:
        raise ConflictError(
            "Your workspace requires multi-factor authentication", code="mfa_required_by_tenant"
        )
    if user.mfa_enabled:
        # Epic 14 (AD-30): a break-glass admin without MFA no longer counts.
        await service.run_admin_loss_guards(db, user.id)
    await _disable(db, user, code)


async def regenerate_user_recovery_codes(
    ctx: RequestContext, db: AsyncSession, code: str
) -> RecoveryCodesOut:
    return await _regenerate(db, await _load_user(ctx, db), code)


async def verify_user_challenge(
    ctx: RequestContext, token: str | None, body: MfaVerifyIn
) -> service.UserAuthResult:
    """Second sign-in step for users: consume the ``tos_mfa`` challenge and open the session.

    Wrong codes are counted (committed) before the 401; the 5th invalidates the challenge.
    """
    found = await _find_challenge(token, "user")
    tenant = await service.active_tenant(found.tenant_id if found is not None else None)
    if found is None or tenant is None:
        raise challenge_expired()
    now = _now()
    error: Unauthorized | None = None
    result: service.UserAuthResult | None = None
    async with tenant_session(tenant.id) as db:
        challenge = await db.get(MfaChallenge, found.id, with_for_update=True, populate_existing=True)
        user = await db.get(User, found.principal_id, with_for_update=True)
        if challenge is None or not _challenge_usable(challenge, now):
            error = challenge_expired()
        elif user is None or not user.is_active or not user.mfa_enabled:
            challenge.consumed_at = now
            error = challenge_expired()
        else:
            audit.set_actor(db, "user", user.id)
            method = await _second_factor(db, user, body, now)
            if method is None:
                error = _count_failure(db, challenge, _subject(user), now)
            else:
                challenge.consumed_at = now
                result = await service.open_user_sign_in(ctx, db, user, tenant, now, method=f"mfa_{method}")
    if error is not None or result is None:
        raise error or challenge_expired()
    return result


# ================================================================ platform operators
async def _load_operator(ctx: RequestContext, db: AsyncSession) -> PlatformOperator:
    principal = service.require_platform(ctx)
    operator = await db.get(PlatformOperator, principal.id, with_for_update=True)
    if operator is None:  # pragma: no cover - authenticated principal always exists
        raise NotFound("Operator not found")
    return operator


async def enroll_operator(ctx: RequestContext, db: AsyncSession) -> MfaEnrollOut:
    operator = await _load_operator(ctx, db)
    return await _start_enrollment(db, operator, f"{operator.email} (platform)")


async def confirm_operator_enrollment(ctx: RequestContext, db: AsyncSession, code: str) -> RecoveryCodesOut:
    return await _confirm_enrollment(db, await _load_operator(ctx, db), code)


async def disable_operator_mfa(ctx: RequestContext, db: AsyncSession, code: str) -> None:
    await _disable(db, await _load_operator(ctx, db), code)


async def regenerate_operator_recovery_codes(
    ctx: RequestContext, db: AsyncSession, code: str
) -> RecoveryCodesOut:
    return await _regenerate(db, await _load_operator(ctx, db), code)


async def verify_operator_challenge(
    ctx: RequestContext, token: str | None, body: MfaVerifyIn
) -> service.OperatorLoginResult:
    found = await _find_challenge(token, "platform")
    if found is None:
        raise challenge_expired()
    now = _now()
    error: Unauthorized | None = None
    result: service.OperatorLoginResult | None = None
    async with platform_session() as db:
        challenge = await db.get(MfaChallenge, found.id, with_for_update=True, populate_existing=True)
        operator = await db.get(PlatformOperator, found.principal_id, with_for_update=True)
        if challenge is None or not _challenge_usable(challenge, now):
            error = challenge_expired()
        elif operator is None or not operator.is_active or not operator.mfa_enabled:
            challenge.consumed_at = now
            error = challenge_expired()
        else:
            method = await _second_factor(db, operator, body, now)
            if method is None:
                error = _count_failure(db, challenge, _subject(operator), now)
            else:
                challenge.consumed_at = now
                result = await service.open_operator_session(ctx, db, operator, now, method=f"mfa_{method}")
    if error is not None or result is None:
        raise error or challenge_expired()
    return result


# ================================================================ tenant security settings
async def get_tenant_security(ctx: RequestContext, db: AsyncSession) -> TenantSecurityOut:
    principal = service.require_user(ctx)
    tenant = await db.get(Tenant, principal.tenant_id)
    if tenant is None:  # pragma: no cover
        raise NotFound("Tenant not found")
    return TenantSecurityOut(mfa_required=tenant.mfa_required)


async def update_tenant_security(
    ctx: RequestContext, db: AsyncSession, data: TenantSecurityUpdate
) -> TenantSecurityOut:
    """Toggle "require MFA". Users without MFA get restricted sessions from their next request."""
    principal = service.require_user(ctx)
    tenant = await db.get(Tenant, principal.tenant_id, with_for_update=True)
    if tenant is None:  # pragma: no cover
        raise NotFound("Tenant not found")
    if tenant.mfa_required != data.mfa_required:
        audit.record(
            "tenant.security_updated",
            "tenant",
            tenant.id,
            session=db,
            mfa_required={"old": tenant.mfa_required, "new": data.mfa_required},
        )
        tenant.mfa_required = data.mfa_required
        await db.flush()
    return TenantSecurityOut(mfa_required=tenant.mfa_required)
