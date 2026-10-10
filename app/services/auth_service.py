"""Identity service: the module's public API (AD-1).

* Generic opaque sessions for every principal type (``create_session`` / ``resolve_session`` /
  ``revoke_session``) -- reused by users (1.3) and candidates (9.3).
* Sign-in lockout shared by users and platform operators (``check_login_attempt``).
* Platform operators: creation (CLI), sign-in/out, ``require_platform_operator`` dependency.
* Tenants: provisioning (tenant + first-admin invitation + email + ``TenantProvisioned``), list, get.
* Invitations: creation, the single-use ``claim_invitation`` primitive and acceptance (1.3).
* Tenant users (1.3): sign-in/out, ``authenticate_user`` (behind ``identity.dependencies.current_user``),
  ``get_me``, password reset by emailed single-use link.
* RBAC (1.5): provisioning seeds the default roles; the authenticated ``Principal`` carries the user's
  resolved permissions, teams and business unit. Administration lives in ``identity.rbac``.
"""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import BackgroundTasks, Request
from loguru import logger
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext, update_context
from app.core.database import platform_session, tenant_session
from app.core.errors import ConflictError, Forbidden, Gone, NotFound, Unauthorized, ValidationFailed
from app.core.events import emit
from app.core.mailer import EmailMessage, send_email
from app.core.pagination import Page, PageParams, paginate
from app.core.passwords import password_policy_violation
from app.core.permissions import TENANT_ADMIN
from app.core.security import (
    PLATFORM_COOKIE,
    USER_COOKIE,
    Principal,
    PrincipalType,
    clear_auth_cookies,
    csrf_token_for,
    hash_password,
    hash_token,
    new_token,
    password_needs_rehash,
    verify_csrf,
    verify_password,
)
from app.core.settings import get_settings
from app.events.identity import TenantProvisioned
from app.models.identity import (
    Invitation,
    LoginThrottle,
    MfaChallenge,
    PasswordReset,
    PlatformOperator,
    Tenant,
    User,
)
from app.models.identity import Session as AuthSession
from app.schemas.identity import (
    AuthOut,
    Email,
    InvitationAccept,
    InvitationOut,
    OperatorLogin,
    OperatorOut,
    PasswordResetConfirm,
    PasswordResetRequest,
    TenantCreate,
    TenantOut,
    TenantSummary,
    UserLogin,
    UserOut,
)
from app.services import access_service as access

TENANT_ADMIN_ROLE = TENANT_ADMIN
# request.state attribute caching the authenticated principal (several dependencies authenticate).
PRINCIPAL_STATE_ATTR = "tos_principal"
TENANT_SLUG_CONSTRAINT = "uq_tenants_slug"
OPERATOR_EMAIL_CONSTRAINT = "uq_platform_operators_email"
USER_EMAIL_CONSTRAINT = "uq_users_tenant_id_email"
MAX_NAME_LENGTH = 200
_EMAIL_ADAPTER: TypeAdapter[str] = TypeAdapter(Email)
# last_seen_at is written at most this often, so authenticated reads don't all become writes.
LAST_SEEN_RESOLUTION = timedelta(seconds=60)
_USER_AGENT_MAX = 512
# Sign-in lockout (users and platform operators): LOCKOUT_THRESHOLD failures within LOCKOUT_WINDOW lock
# the account for LOCKOUT_DURATION.
LOCKOUT_THRESHOLD = 5
LOCKOUT_WINDOW = timedelta(minutes=15)
LOCKOUT_DURATION = timedelta(minutes=15)
PASSWORD_RESET_TTL = timedelta(hours=1)
# A new reset link is issued at most once per cooldown while an earlier one is unused.
PASSWORD_RESET_COOLDOWN = timedelta(seconds=60)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ================================================================ sessions (generic)
@dataclasses.dataclass(frozen=True, slots=True)
class IssuedSession:
    session: AuthSession
    token: str  # raw cookie value; never persisted
    max_age: int  # seconds, for the cookie


async def create_session(
    db: AsyncSession,
    *,
    principal_type: PrincipalType,
    principal_id: uuid.UUID,
    tenant_id: uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    absolute_timeout: timedelta | None = None,
    mfa_enrollment_required: bool = False,
) -> IssuedSession:
    absolute = (
        absolute_timeout
        if absolute_timeout is not None
        else timedelta(days=get_settings().session_absolute_timeout_days)
    )
    if absolute <= timedelta(0):
        msg = "absolute_timeout must be positive"
        raise ValueError(msg)
    now = _now()
    token = new_token()
    row = AuthSession(
        token_hash=hash_token(token),
        principal_type=principal_type,
        principal_id=principal_id,
        tenant_id=tenant_id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + absolute,
        ip=ip,
        user_agent=user_agent[:_USER_AGENT_MAX] if user_agent else None,
        mfa_enrollment_required=mfa_enrollment_required,
    )
    db.add(row)
    await db.flush()
    return IssuedSession(session=row, token=token, max_age=int(absolute.total_seconds()))


def session_is_live(row: AuthSession, now: datetime, idle_timeout: timedelta) -> bool:
    return row.revoked_at is None and now < row.expires_at and now - row.last_seen_at <= idle_timeout


async def find_session(db: AsyncSession, principal_type: PrincipalType, token: str) -> AuthSession | None:
    """Return the session row for ``token`` regardless of liveness (callers apply ``session_is_live``)."""
    if not token:
        return None
    return (
        await db.execute(
            select(AuthSession).where(
                AuthSession.token_hash == hash_token(token), AuthSession.principal_type == principal_type
            )
        )
    ).scalar_one_or_none()


async def touch_session(db: AsyncSession, row: AuthSession, now: datetime) -> None:
    """Advance ``last_seen_at``, at most once per ``LAST_SEEN_RESOLUTION``."""
    if now - row.last_seen_at >= LAST_SEEN_RESOLUTION:
        row.last_seen_at = now
        await db.flush()


async def resolve_session(
    db: AsyncSession, principal_type: PrincipalType, token: str, *, idle_timeout: timedelta | None = None
) -> AuthSession | None:
    """Return the live session for ``token`` (touching ``last_seen_at``), or None if unknown/expired/revoked.

    Rejected when revoked, past ``expires_at`` (absolute), or idle longer than ``idle_timeout``
    (default ``SESSION_IDLE_TIMEOUT_HOURS``).
    """
    idle = (
        idle_timeout
        if idle_timeout is not None
        else timedelta(hours=get_settings().session_idle_timeout_hours)
    )
    row = await find_session(db, principal_type, token)
    now = _now()
    if row is None or not session_is_live(row, now, idle):
        return None
    await touch_session(db, row, now)
    return row


async def revoke_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(
        update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


async def revoke_principal_sessions(
    db: AsyncSession, principal_type: PrincipalType, principal_id: uuid.UUID
) -> int:
    result = await db.execute(
        update(AuthSession)
        .where(
            AuthSession.principal_type == principal_type,
            AuthSession.principal_id == principal_id,
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=_now())
    )
    return int(result.rowcount)


def _session_rejected(cookie_name: str) -> Unauthorized:
    exc = Unauthorized("Session expired or revoked", code="session_expired")
    exc.response_hooks.append(lambda response: clear_auth_cookies(response, cookie_name))
    return exc


# ================================================================ MFA challenges (story 1.4)
MFA_CHALLENGE_TTL = timedelta(minutes=5)


@dataclasses.dataclass(frozen=True, slots=True)
class MfaChallengeResult:
    """Password accepted for an MFA principal: the raw ``tos_mfa`` cookie token (never persisted)."""

    token: str
    max_age: int


async def issue_mfa_challenge(
    db: AsyncSession, principal_type: PrincipalType, principal_id: uuid.UUID, tenant_id: uuid.UUID | None
) -> MfaChallengeResult:
    """Store a single-use challenge (hash only) valid ``MFA_CHALLENGE_TTL``. ``mfa_challenges`` is a platform
    table, so any session may write it."""
    token = new_token()
    db.add(
        MfaChallenge(
            token_hash=hash_token(token),
            principal_type=principal_type,
            principal_id=principal_id,
            tenant_id=tenant_id,
            expires_at=_now() + MFA_CHALLENGE_TTL,
        )
    )
    await db.flush()
    return MfaChallengeResult(token=token, max_age=int(MFA_CHALLENGE_TTL.total_seconds()))


# ================================================================ credentials, password policy, lockout
def violated_constraint(exc: IntegrityError) -> str | None:
    """Name of the constraint behind an IntegrityError (asyncpg exposes ``constraint_name``)."""
    todo: list[BaseException | None] = [exc.orig, exc.__cause__]
    seen: list[BaseException] = []
    while todo:
        cur = todo.pop()
        if cur is None or any(cur is x for x in seen):
            continue
        seen.append(cur)
        name = getattr(cur, "constraint_name", None)
        if isinstance(name, str):
            return name
        todo.extend([cur.__cause__, getattr(cur, "orig", None)])
    return None


def invalid_credentials() -> Unauthorized:
    return Unauthorized("Invalid email or password", code="invalid_credentials")


def account_locked(locked_until: datetime) -> Unauthorized:
    return Unauthorized(
        "Too many failed sign-in attempts; try again later",
        code="account_locked",
        details={"locked_until": _iso(locked_until)},
    )


def validate_password_strength(password: str) -> None:
    """Raise 422 ``weak_password`` unless the password meets the policy.

    ``details.fields`` has the same list shape as request-validation errors (``core.errors``).
    """
    reason = password_policy_violation(password)
    if reason is not None:
        raise ValidationFailed(
            reason,
            code="weak_password",
            details={"fields": [{"field": "password", "message": reason, "type": "weak_password"}]},
        )


@dataclasses.dataclass(frozen=True, slots=True)
class LoginCheck:
    outcome: Literal["ok", "failed", "inactive", "locked"]
    locked_until: datetime | None = None

    def error(self) -> Unauthorized:
        if self.outcome == "locked" and self.locked_until is not None:
            return account_locked(self.locked_until)
        return invalid_credentials()

    @property
    def audit_action(self) -> str:
        return {
            "ok": "auth.login_succeeded",
            "failed": "auth.login_failed",
            "inactive": "auth.login_failed",
            "locked": "auth.login_locked",
        }[self.outcome]


def check_login_attempt(
    account: LoginThrottle | None, *, password_hash: str | None, is_active: bool, password: str, now: datetime
) -> LoginCheck:
    """Verify ``password`` under the lockout policy, mutating ``account``'s counters.

    * unknown or deactivated account: the password is still verified (dummy / stored hash) so timing is
      uniform; nothing is counted and the lock state is never revealed -> ``invalid_credentials``;
    * locked account: the password is NOT evaluated (a dummy verification keeps the timing) -> identical
      ``account_locked`` with ``details.locked_until`` whatever password was sent (no oracle while locked);
    * wrong password: counted in a ``LOCKOUT_WINDOW`` window starting at the first failure; the
      ``LOCKOUT_THRESHOLD``-th failure locks the account for ``LOCKOUT_DURATION`` -> ``account_locked``;
    * correct password: counters reset.

    Callers load ``account`` ``FOR UPDATE`` and commit even when the attempt fails.
    """
    if account is None or not is_active:
        verify_password(password_hash if account is not None else None, password)
        return LoginCheck("failed" if account is None else "inactive")
    if account.locked_until is not None and account.locked_until > now:
        verify_password(None, password)
        return LoginCheck("locked", account.locked_until)
    if not verify_password(password_hash, password):
        if account.first_failed_at is None or now - account.first_failed_at > LOCKOUT_WINDOW:
            account.first_failed_at = now
            account.failed_login_count = 1
        else:
            account.failed_login_count += 1
        if account.failed_login_count >= LOCKOUT_THRESHOLD:
            account.locked_until = now + LOCKOUT_DURATION
            account.failed_login_count = 0
            account.first_failed_at = None
            return LoginCheck("locked", account.locked_until)
        return LoginCheck("failed")
    reset_login_throttle(account)
    return LoginCheck("ok")


def reset_login_throttle(account: LoginThrottle) -> None:
    account.failed_login_count = 0
    account.first_failed_at = None
    account.locked_until = None


# ================================================================ platform operators
@dataclasses.dataclass(frozen=True, slots=True)
class OperatorLoginResult:
    operator: OperatorOut
    session_token: str
    csrf_token: str
    max_age: int


async def create_operator(email: str, name: str, password: str) -> OperatorOut:
    """Create a platform operator (used by ``python -m app.cli create-operator``)."""
    try:
        email = str(_EMAIL_ADAPTER.validate_python(email.strip())).lower()
    except PydanticValidationError as exc:
        raise ValidationFailed("A valid email is required", code="invalid_email") from exc
    name = name.strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        raise ValidationFailed(
            f"Name is required (at most {MAX_NAME_LENGTH} characters)", code="invalid_name"
        )
    validate_password_strength(password)
    try:
        async with platform_session() as db:
            exists = (
                await db.execute(select(PlatformOperator.id).where(PlatformOperator.email == email))
            ).first()
            if exists:
                raise ConflictError(
                    "A platform operator with this email already exists", code="operator_exists"
                )
            operator = PlatformOperator(email=email, name=name, password_hash=hash_password(password))
            db.add(operator)
            await db.flush()
            await db.refresh(operator)
            out = OperatorOut.model_validate(operator)
            audit.record("platform_operator.created", "platform_operator", operator.id, session=db)
    except IntegrityError as exc:  # concurrent create with the same email
        if violated_constraint(exc) != OPERATOR_EMAIL_CONSTRAINT:
            raise
        raise ConflictError(
            "A platform operator with this email already exists", code="operator_exists"
        ) from exc
    return out


async def open_operator_session(
    ctx: RequestContext, db: AsyncSession, operator: PlatformOperator, now: datetime, *, method: str
) -> OperatorLoginResult:
    """Complete an operator sign-in (password, or password + MFA): session, ``last_login_at``, audit."""
    operator.last_login_at = now
    issued = await create_session(
        db, principal_type="platform", principal_id=operator.id, ip=ctx.ip, user_agent=ctx.user_agent
    )
    await db.flush()
    profile = OperatorOut.model_validate(operator)
    audit.record(
        "auth.login_succeeded",
        "platform_operator",
        profile.id,
        session=db,
        actor_type="platform",
        actor_id=profile.id,
        method=method,
    )
    return OperatorLoginResult(
        operator=profile,
        session_token=issued.token,
        csrf_token=csrf_token_for(issued.token),
        max_age=issued.max_age,
    )


async def login_operator(
    ctx: RequestContext, data: OperatorLogin
) -> OperatorLoginResult | MfaChallengeResult:
    """Verify credentials and open a platform session (lockout policy: ``check_login_attempt``), or -- for
    an operator with MFA -- issue an MFA challenge instead (story 1.4).

    Lockout counters are committed before a failed attempt's 401 is raised.
    """
    now = _now()
    error: Unauthorized | None = None
    result: OperatorLoginResult | MfaChallengeResult | None = None
    async with platform_session() as db:
        operator = (
            await db.execute(
                select(PlatformOperator).where(PlatformOperator.email == data.email.lower()).with_for_update()
            )
        ).scalar_one_or_none()
        operator_id = operator.id if operator else None
        check = check_login_attempt(
            operator,
            password_hash=operator.password_hash if operator else None,
            is_active=operator.is_active if operator else False,
            password=data.password,
            now=now,
        )
        if operator is None or check.outcome != "ok":
            error = check.error()
            audit.record(
                check.audit_action,
                "platform_operator",
                operator_id,
                session=db,
                actor_type="platform",
                actor_id=operator_id,
                reason=check.outcome,
            )
        else:
            if password_needs_rehash(operator.password_hash):
                operator.password_hash = hash_password(data.password)
            if operator.mfa_enabled:
                result = await issue_mfa_challenge(db, "platform", operator.id, None)
                audit.record(
                    "auth.mfa_challenge_issued",
                    "platform_operator",
                    operator.id,
                    session=db,
                    actor_type="platform",
                    actor_id=operator.id,
                )
            else:
                result = await open_operator_session(ctx, db, operator, now, method="password")
    if error is not None or result is None:
        logger.info("Platform operator sign-in failed")
        raise error or invalid_credentials()
    return result


async def logout(ctx: RequestContext) -> None:
    """Revoke the caller's current session (any principal type) and audit the sign-out."""
    principal = ctx.principal
    if isinstance(principal, Principal) and principal.session_id is not None:
        # ``sessions`` is a platform table (no RLS); a tenant user's audit row belongs to its tenant.
        cm = tenant_session(principal.tenant_id) if principal.tenant_id is not None else platform_session()
        async with cm as db:
            await revoke_session(db, principal.session_id)
            audit.record(
                "auth.logout",
                "session",
                principal.session_id,
                session=db,
                # Support sessions (Epic 12) are operator actions.
                actor_type="platform" if principal.type == "support" else principal.type,
                actor_id=principal.id,
            )


async def get_operator_profile(ctx: RequestContext, db: AsyncSession) -> OperatorOut:
    principal = require_platform(ctx)
    operator = await db.get(PlatformOperator, principal.id)
    if operator is None:
        raise NotFound("Operator not found")
    return OperatorOut.model_validate(operator)


async def require_platform_operator(request: Request) -> Principal:
    """FastAPI dependency: authenticate the ``tos_platform`` cookie, enforce CSRF on mutations.

    Order: no cookie -> 401; stale/revoked/unknown cookie -> 401 + cookies cleared; then CSRF -> 403.
    """
    token = request.cookies.get(PLATFORM_COOKIE)
    if not token:
        raise Unauthorized()
    principal: Principal | None = None
    async with platform_session() as db:
        row = await resolve_session(db, "platform", token)
        if row is not None:
            operator = await db.get(PlatformOperator, row.principal_id)
            if operator is None or not operator.is_active:
                await revoke_session(db, row.id)
            else:
                principal = Principal(type="platform", id=operator.id, session_id=row.id)
    if principal is None:
        raise _session_rejected(PLATFORM_COOKIE)
    verify_csrf(request)
    update_context(principal=principal)
    return principal


def require_platform(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "platform":
        raise Unauthorized()
    return principal


# ================================================================ tenant-prefixed single-use tokens
def tenant_token(tenant_id: uuid.UUID) -> str:
    """``<tenant uuid hex>.<32 random bytes>``: the tenant prefix lets the link's consumer open the right
    RLS-scoped session before looking the token hash up. Only the SHA-256 of the whole token is stored.
    Used for invitations and password resets."""
    return f"{tenant_id.hex}.{new_token()}"


def parse_tenant_token(token: str) -> uuid.UUID | None:
    """Tenant id encoded in a ``tenant_token``, or None if the token is malformed."""
    prefix, sep, secret = token.partition(".")
    if not sep or len(prefix) != 32 or len(secret) < 22:
        return None
    try:
        return uuid.UUID(hex=prefix)
    except ValueError:
        return None


invitation_token = tenant_token
parse_invitation_token = parse_tenant_token


async def active_tenant(tenant_id: uuid.UUID | None) -> Tenant | None:
    if tenant_id is None:
        return None
    async with platform_session() as db:
        tenant = await db.get(Tenant, tenant_id)
    return tenant if tenant is not None and tenant.status == "active" else None


# Epic 12 (FR-70): suspended tenants get 403 ``tenant_suspended`` at sign-in; offboarding tenants admit
# tenant admins only, read-only (``authenticate_user``).
SIGN_IN_TENANT_STATUSES: tuple[str, ...] = ("active", "suspended", "offboarding")
SESSION_TENANT_STATUSES: tuple[str, ...] = ("active", "offboarding")


async def _active_tenant_by_slug(slug: str, allow: tuple[str, ...] = ("active",)) -> Tenant | None:
    async with platform_session() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    return tenant if tenant is not None and tenant.status in allow else None


async def tenant_by_slug(slug: str) -> Tenant | None:
    """Epic 14: the active tenant with ``slug`` (SSO, Client / Vendor User sign-in)."""
    return await _active_tenant_by_slug(slug.strip().lower())


# Epic 14 (AD-30): the SSO domain registers (from its hooks) a password gate -- password sign-in refused while
# SSO is enforced for the user, break-glass admins excepted -- and a guard run before a Tenant Admin is
# demoted / deactivated / loses MFA. Registries keep identity free of imports of the upper ``sso`` domain.
PasswordGate = Callable[[AsyncSession, User], Awaitable[bool]]
AdminLossGuard = Callable[[AsyncSession, uuid.UUID], Awaitable[None]]
_password_gates: dict[str, PasswordGate] = {}
_admin_loss_guards: dict[str, AdminLossGuard] = {}


def register_password_gate(key: str, gate: PasswordGate) -> None:
    _password_gates[key] = gate


def register_admin_loss_guard(key: str, guard: AdminLossGuard) -> None:
    _admin_loss_guards[key] = guard


async def _sso_password_allowed(db: AsyncSession, user: User) -> bool:
    for gate in _password_gates.values():
        if not await gate(db, user):
            return False
    return True


async def run_admin_loss_guards(db: AsyncSession, losing_user_id: uuid.UUID) -> None:
    """Raise (e.g. 409 ``break_glass_required``) when ``losing_user_id`` may not lose admin access now."""
    for guard in _admin_loss_guards.values():
        await guard(db, losing_user_id)


# ================================================================ invitations
@dataclasses.dataclass(frozen=True, slots=True)
class IssuedInvitation:
    invitation: Invitation
    token: str  # raw token for the emailed link; never persisted


def accept_invite_url(token: str) -> str:
    # Epic 12 (A10): the token travels in the URL fragment, never sent to servers or logged.
    return f"{get_settings().frontend_url.rstrip('/')}/accept-invite#token={token}"


async def create_invitation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    email: str,
    role_ids: Sequence[uuid.UUID],
    invited_by_type: str,
    invited_by_id: uuid.UUID,
    name: str | None = None,
    business_unit_id: uuid.UUID | None = None,
    team_ids: Sequence[uuid.UUID] = (),
) -> IssuedInvitation:
    """Insert an invitation. ``db`` must be a ``tenant_session(tenant_id)`` (RLS WITH CHECK).

    Callers validate ``role_ids`` / ``business_unit_id`` / ``team_ids`` against the tenant.
    """
    token = invitation_token(tenant_id)
    invitation = Invitation(
        tenant_id=tenant_id,
        email=email.strip().lower(),
        name=name,
        role_ids=list(dict.fromkeys(role_ids)),
        business_unit_id=business_unit_id,
        team_ids=list(dict.fromkeys(team_ids)),
        token_hash=hash_token(token),
        expires_at=_now() + timedelta(hours=get_settings().invitation_ttl_hours),
        invited_by_type=invited_by_type,
        invited_by_id=invited_by_id,
    )
    db.add(invitation)
    await db.flush()
    return IssuedInvitation(invitation=invitation, token=token)


def rotate_invitation_token(invitation: Invitation) -> str:
    """Give a pending invitation a fresh token and expiry (the old link stops working); returns the token."""
    token = invitation_token(invitation.tenant_id)
    invitation.token_hash = hash_token(token)
    invitation.expires_at = _now() + timedelta(hours=get_settings().invitation_ttl_hours)
    invitation.revoked_at = None
    return token


async def claim_invitation(db: AsyncSession, token: str) -> Invitation | None:
    """Atomically mark the invitation for ``token`` accepted; None if unknown, expired, revoked or used.

    ``db`` must be a tenant session for ``parse_invitation_token(token)``; the conditional UPDATE
    guarantees at most one successful claim.
    """
    now = _now()
    result = await db.execute(
        update(Invitation)
        .where(
            Invitation.token_hash == hash_token(token),
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > now,
        )
        .values(accepted_at=now)
        .returning(Invitation)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    return result.scalar_one_or_none()


def _invitation_invalid() -> Gone:
    return Gone("This invitation link is invalid, expired or already used", code="invitation_invalid")


async def _invitation_tenant(token: str) -> Tenant:
    tenant = await active_tenant(parse_invitation_token(token))
    if tenant is None:
        raise _invitation_invalid()
    return tenant


async def get_invitation(ctx: RequestContext, token: str) -> InvitationOut:  # noqa: ARG001
    """Public preview of a pending invitation (tenant name + invited email) for the accept page."""
    tenant = await _invitation_tenant(token)
    async with tenant_session(tenant.id) as db:
        invitation = (
            await db.execute(
                select(Invitation).where(
                    Invitation.token_hash == hash_token(token),
                    Invitation.accepted_at.is_(None),
                    Invitation.revoked_at.is_(None),
                    Invitation.expires_at > _now(),
                )
            )
        ).scalar_one_or_none()
    if invitation is None:
        raise _invitation_invalid()
    return InvitationOut(
        email=invitation.email,
        name=invitation.name,
        tenant=TenantSummary.model_validate(tenant),
        expires_at=invitation.expires_at,
    )


def tenant_admin_invitation_email(tenant: Tenant, email: str, token: str) -> EmailMessage:
    link = accept_invite_url(token)
    hours = get_settings().invitation_ttl_hours
    text = (
        f"Hello,\n\n"
        f"You have been invited to set up and administer {tenant.name} on Talent OS.\n\n"
        f"Accept the invitation and create your account here:\n{link}\n\n"
        f"This link can be used once and expires in {hours} hours.\n"
        f"If you were not expecting this invitation, you can ignore this email.\n"
    )
    return EmailMessage(
        to=email,
        subject=f"You're invited to administer {tenant.name} on Talent OS",
        text=text,
        tenant_id=tenant.id,
        kind="tenant_admin_invitation",
    )


# ================================================================ tenant users
@dataclasses.dataclass(frozen=True, slots=True)
class UserAuthResult:
    auth: AuthOut
    session_token: str
    csrf_token: str
    max_age: int


def session_timeouts(tenant: Tenant) -> tuple[timedelta, timedelta]:
    """``(idle, absolute)`` session timeouts: the tenant's overrides, else the platform defaults."""
    s = get_settings()
    idle = (
        timedelta(minutes=tenant.session_idle_minutes)
        if tenant.session_idle_minutes
        else timedelta(hours=s.session_idle_timeout_hours)
    )
    absolute = (
        timedelta(hours=tenant.session_absolute_hours)
        if tenant.session_absolute_hours
        else timedelta(days=s.session_absolute_timeout_days)
    )
    return idle, absolute


def mfa_enrollment_required(user: User, tenant: Tenant) -> bool:
    """Return True when the tenant requires MFA and the user has not enrolled (restricted session)."""
    return tenant.mfa_required and not user.mfa_enabled


async def _open_user_session(
    ctx: RequestContext, db: AsyncSession, user: User, tenant: Tenant
) -> IssuedSession:
    _, absolute = session_timeouts(tenant)
    return await create_session(
        db,
        principal_type="user",
        principal_id=user.id,
        tenant_id=tenant.id,
        ip=ctx.ip,
        user_agent=ctx.user_agent,
        absolute_timeout=absolute,
        mfa_enrollment_required=mfa_enrollment_required(user, tenant),
    )


async def open_user_sign_in(
    ctx: RequestContext, db: AsyncSession, user: User, tenant: Tenant, now: datetime, *, method: str
) -> "UserAuthResult":
    """Complete a user sign-in (password, or password + MFA): session, ``last_login_at``, audit."""
    user.last_login_at = now
    issued = await _open_user_session(ctx, db, user, tenant)
    await db.flush()
    auth = _auth_out(user, tenant, await access.load_access(db, user.id))
    audit.record("auth.login_succeeded", "user", user.id, session=db, method=method)
    return UserAuthResult(
        auth=auth, session_token=issued.token, csrf_token=csrf_token_for(issued.token), max_age=issued.max_age
    )


def user_out(user: User, role_keys: Sequence[str]) -> UserOut:
    return UserOut(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        name=user.name,
        role_keys=list(role_keys),
        business_unit_id=user.business_unit_id,
        mfa_enabled=user.mfa_enabled,
        last_login_at=user.last_login_at,
    )


def _auth_out(user: User, tenant: Tenant, user_access: access.Access) -> AuthOut:
    return AuthOut(
        user=user_out(user, user_access.role_keys),
        tenant=TenantSummary.model_validate(tenant),
        permissions={perm: scope.value for perm, scope in sorted(user_access.permissions.items())},
        mfa_enrollment_required=mfa_enrollment_required(user, tenant),
    )


async def accept_invitation(ctx: RequestContext, token: str, data: InvitationAccept) -> UserAuthResult:
    """Consume the invitation, create the user with the invited roles / business unit / teams and sign
    them in (one transaction). Referenced roles/teams/BU deleted since the invitation are skipped.

    Refused (and the invitation left unused) when the email already has an account or the tenant has no
    free seat (active users >= ``seat_limit``).
    """
    validate_password_strength(data.password)
    tenant = await _invitation_tenant(token)
    try:
        async with tenant_session(tenant.id) as db:
            # Serialize seat consumption: concurrent accepts must not both see a free seat.
            locked = await access.lock_tenant(db, tenant.id)
            invitation = await claim_invitation(db, token)
            if invitation is None or locked is None:
                raise _invitation_invalid()
            exists = (await db.execute(select(User.id).where(User.email == invitation.email))).first()
            if exists:
                raise ConflictError("An account with this email already exists", code="user_exists")
            if await access.active_user_count(db) >= locked.seat_limit:
                raise ConflictError("This workspace has no free seats", code="seat_limit_reached")
            business_unit_id = invitation.business_unit_id
            if business_unit_id is not None and not await access.business_unit_exists(db, business_unit_id):
                business_unit_id = None
            user = User(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                email=invitation.email,
                name=data.name,
                password_hash=hash_password(data.password),
                business_unit_id=business_unit_id,
                last_login_at=_now(),
            )
            audit.set_actor(db, "user", user.id)  # the new user performs its own sign-up
            db.add(user)
            await db.flush()
            await db.refresh(user)
            role_ids = await access.existing_role_ids(db, invitation.role_ids)
            await access.set_user_roles(db, tenant.id, user.id, role_ids)
            team_ids = await access.existing_team_ids(db, invitation.team_ids)
            await access.set_user_teams(db, tenant.id, user.id, team_ids)
            issued = await _open_user_session(ctx, db, user, tenant)
            auth = _auth_out(user, tenant, await access.load_access(db, user.id))
            audit.record("auth.invitation_accepted", "user", user.id, session=db, invitation_id=invitation.id)
    except IntegrityError as exc:  # concurrent acceptance of two invitations for the same email
        if violated_constraint(exc) != USER_EMAIL_CONSTRAINT:
            raise
        raise ConflictError("An account with this email already exists", code="user_exists") from exc
    return UserAuthResult(
        auth=auth, session_token=issued.token, csrf_token=csrf_token_for(issued.token), max_age=issued.max_age
    )


async def login_user(ctx: RequestContext, data: UserLogin) -> UserAuthResult | MfaChallengeResult:
    """Sign a tenant user in (lockout policy: ``check_login_attempt``).

    Unknown/suspended tenant, unknown email, wrong password and deactivated user all return 401
    ``invalid_credentials``; a locked account returns 401 ``account_locked`` without evaluating the
    password. Lockout counters are committed before the 401 is raised.
    """
    tenant = await _active_tenant_by_slug(data.tenant_slug, allow=SIGN_IN_TENANT_STATUSES)
    if tenant is not None and tenant.status == "suspended":
        # Epic 12 (FR-70): checked before the password (no hashing for a suspended workspace).
        raise Forbidden("This workspace is suspended", code="tenant_suspended")
    if tenant is None:
        verify_password(None, data.password)  # same cost as a real check
        async with platform_session() as pdb:
            audit.record(
                "auth.login_failed",
                "user",
                None,
                session=pdb,
                actor_type="user",
                actor_id=None,
                reason="unknown_tenant",
            )
        raise invalid_credentials()

    now = _now()
    error: Unauthorized | Forbidden | None = None
    result: UserAuthResult | MfaChallengeResult | None = None
    async with tenant_session(tenant.id) as db:
        user = (
            await db.execute(select(User).where(User.email == data.email.lower()).with_for_update())
        ).scalar_one_or_none()
        user_id = user.id if user else None
        audit.set_actor(db, "user", user_id)
        check = check_login_attempt(
            user,
            password_hash=user.password_hash if user else None,
            is_active=user.is_active if user else False,
            password=data.password,
            now=now,
        )
        offboarding_non_admin = (
            user is not None
            and check.outcome == "ok"
            and tenant.status == "offboarding"
            and TENANT_ADMIN_ROLE not in (await access.load_access(db, user.id)).role_keys
        )
        if user is None or check.outcome != "ok":
            error = check.error()
            audit.record(check.audit_action, "user", user_id, session=db, reason=check.outcome)
        elif offboarding_non_admin:
            error = Forbidden("This workspace is being offboarded", code="tenant_offboarding")
            audit.record("auth.login_failed", "user", user_id, session=db, reason="tenant_offboarding")
        elif not await _sso_password_allowed(db, user):
            # Epic 14: checked after the password (same cost as a wrong password), before any session.
            error = Forbidden("Sign in with your organization's single sign-on", code="sso_required")
            audit.record("auth.login_failed", "user", user_id, session=db, reason="sso_required")
        else:
            if password_needs_rehash(user.password_hash):
                user.password_hash = hash_password(data.password)
            if user.mfa_enabled:
                result = await issue_mfa_challenge(db, "user", user.id, tenant.id)
                audit.record("auth.mfa_challenge_issued", "user", user.id, session=db)
            else:
                result = await open_user_sign_in(ctx, db, user, tenant, now, method="password")
    if error is not None or result is None:
        raise error or invalid_credentials()
    return result


# Endpoints usable while a user must still enrol in MFA (tenant ``mfa_required``, story 1.4).
MFA_ENROLLMENT_PATHS: frozenset[str] = frozenset({"/api/v1/auth/me", "/api/v1/auth/logout"})
MFA_ENROLLMENT_PREFIX = "/api/v1/auth/mfa/"


def _enrollment_allowed(path: str) -> bool:
    return path in MFA_ENROLLMENT_PATHS or path.startswith(MFA_ENROLLMENT_PREFIX)


def user_session_is_live(row: AuthSession, tenant: Tenant, now: datetime) -> bool:
    """Liveness under the tenant's *current* timeouts: idle, ``expires_at``, and age <= absolute limit
    (so lowering ``session_absolute_hours`` also shortens existing sessions)."""
    idle, absolute = session_timeouts(tenant)
    return session_is_live(row, now, idle) and now - row.created_at <= absolute


async def authenticate_user(request: Request) -> Principal:
    """Authenticate the ``tos_session`` cookie (behind ``identity.dependencies.current_user``).

    Order: no cookie -> 401; unknown/revoked/expired session, suspended tenant or deactivated user (whose
    session is revoked) -> 401 ``session_expired`` + cookies cleared; CSRF on mutations -> 403, checked
    before ``last_seen_at`` is touched. Then the principal -- with the user's resolved permissions, teams
    and business unit -- and tenant go on the request context. The principal is cached on
    ``request.state`` so several dependencies of one request authenticate only once.
    """
    cached = getattr(request.state, PRINCIPAL_STATE_ATTR, None)
    if isinstance(cached, Principal):
        return cached
    token = request.cookies.get(USER_COOKIE)
    if not token:
        raise Unauthorized()
    now = _now()
    live: AuthSession | None = None
    tenant: Tenant | None = None
    async with platform_session() as db:
        row = await find_session(db, "user", token)
        if row is not None and row.tenant_id is not None:
            tenant = await db.get(Tenant, row.tenant_id)
            if (
                tenant is not None
                and tenant.status in SESSION_TENANT_STATUSES
                and user_session_is_live(row, tenant, now)
            ):
                live = row
    if live is None or tenant is None:
        raise _session_rejected(USER_COOKIE)

    principal: Principal | None = None
    async with tenant_session(tenant.id) as tdb:
        user = await tdb.get(User, live.principal_id)  # RLS: a user of another tenant is invisible
        if user is None or not user.is_active:
            await revoke_session(tdb, live.id)
        else:
            verify_csrf(request)  # raising here rolls back: a CSRF failure writes nothing
            if mfa_enrollment_required(user, tenant) and not _enrollment_allowed(request.url.path):
                raise Forbidden(
                    "Set up multi-factor authentication to continue", code="mfa_enrollment_required"
                )
            principal = access.user_principal(
                user_id=user.id,
                tenant_id=tenant.id,
                session_id=live.id,
                business_unit_id=user.business_unit_id,
                access=await access.load_access(tdb, user.id),
            )
            if tenant.status == "offboarding":
                # Epic 12: during the offboarding grace period only tenant admins, read-only.
                if TENANT_ADMIN_ROLE not in (await access.load_access(tdb, user.id)).role_keys:
                    await revoke_session(tdb, live.id)
                    principal = None
                else:
                    principal = dataclasses.replace(principal, read_only=True)
            if principal is not None and now - live.last_seen_at >= LAST_SEEN_RESOLUTION:
                await tdb.execute(
                    update(AuthSession).where(AuthSession.id == live.id).values(last_seen_at=now)
                )
    if principal is None:
        raise _session_rejected(USER_COOKIE)
    update_context(principal=principal, tenant_id=tenant.id)
    setattr(request.state, PRINCIPAL_STATE_ATTR, principal)
    return principal


def require_user(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user" or principal.tenant_id is None:
        raise Unauthorized()
    return principal


async def get_me(ctx: RequestContext, db: AsyncSession) -> AuthOut:
    """Profile of the signed-in user. ``db`` is the request's tenant-scoped session.

    Support-access principals (Epic 12) get their read-only session profile instead."""
    principal = ctx.principal
    if isinstance(principal, Principal) and principal.type == "support":
        from app.services import support_access_service

        return await support_access_service.get_support_me(principal, db)
    principal = require_user(ctx)
    user = await db.get(User, principal.id)
    tenant = await db.get(Tenant, principal.tenant_id)
    if user is None or tenant is None:
        raise Unauthorized()
    return _auth_out(user, tenant, await access.load_access(db, user.id))


# ================================================================ password reset
def reset_password_url(token: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/reset-password#token={token}"


def _reset_invalid() -> Gone:
    return Gone("This password reset link is invalid, expired or already used", code="reset_invalid")


def _password_reset_email(tenant: Tenant, email: str, token: str) -> EmailMessage:
    minutes = int(PASSWORD_RESET_TTL.total_seconds() // 60)
    text = (
        f"Hello,\n\n"
        f"We received a request to reset your {tenant.name} password on Talent OS.\n\n"
        f"Choose a new password here:\n{reset_password_url(token)}\n\n"
        f"This link can be used once and expires in {minutes} minutes. Resetting your password signs you\n"
        f"out everywhere. If you did not ask for this, you can ignore this email.\n"
    )
    return EmailMessage(
        to=email,
        subject=f"Reset your {tenant.name} password on Talent OS",
        text=text,
        tenant_id=tenant.id,
        kind="password_reset",
    )


async def deliver_email(message: EmailMessage) -> None:
    """Background delivery (after the response): failures are logged, never surfaced to the caller."""
    try:
        await send_email(message)
    except Exception:
        logger.exception("Sending email failed (kind={}, tenant_id={})", message.kind, message.tenant_id)


async def request_password_reset(
    ctx: RequestContext,  # noqa: ARG001
    data: PasswordResetRequest,
    background: BackgroundTasks,
) -> None:
    """Email a single-use reset link if the account exists. Always the same 202 (no account enumeration).

    The user row is locked ``FOR UPDATE`` so concurrent requests serialize; if an unused link was issued
    less than ``PASSWORD_RESET_COOLDOWN`` ago nothing happens, otherwise earlier unused links are
    invalidated and a new one is issued. The email is sent as a background task after the response, so no
    branch awaits the mailer.
    """
    tenant = await _active_tenant_by_slug(data.tenant_slug)
    if tenant is None:
        async with platform_session() as pdb:
            audit.record(
                "auth.password_reset_requested",
                "user",
                None,
                session=pdb,
                actor_type="user",
                actor_id=None,
                outcome="unknown_tenant",
            )
        return
    token = tenant_token(tenant.id)
    now = _now()
    user_id: uuid.UUID | None = None
    message: EmailMessage | None = None
    outcome = "no_active_account"
    async with tenant_session(tenant.id) as db:
        user = (
            await db.execute(select(User).where(User.email == data.email.lower()).with_for_update())
        ).scalar_one_or_none()
        if user is not None and user.is_active:
            user_id = user.id
            recent = (
                await db.execute(
                    select(PasswordReset.id)
                    .where(
                        PasswordReset.user_id == user.id,
                        PasswordReset.used_at.is_(None),
                        PasswordReset.created_at > func.now() - PASSWORD_RESET_COOLDOWN,
                    )
                    .limit(1)
                )
            ).first()
            if recent is not None:
                outcome = "throttled"
            else:
                await db.execute(
                    update(PasswordReset)
                    .where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None))
                    .values(used_at=now)
                )
                db.add(
                    PasswordReset(
                        tenant_id=tenant.id,
                        user_id=user.id,
                        token_hash=hash_token(token),
                        expires_at=now + PASSWORD_RESET_TTL,
                    )
                )
                message = _password_reset_email(tenant, user.email, token)
                outcome = "issued"
        audit.record(
            "auth.password_reset_requested",
            "user",
            user_id,
            session=db,
            actor_type="user",
            actor_id=user_id,
            outcome=outcome,
        )
    if message is not None:
        background.add_task(deliver_email, message)


async def confirm_password_reset(ctx: RequestContext, data: PasswordResetConfirm) -> None:  # noqa: ARG001
    """Consume the reset link, set the new password, clear lockout and revoke all the user's sessions."""
    validate_password_strength(data.password)
    tenant = await active_tenant(parse_tenant_token(data.token))
    if tenant is None:
        async with platform_session() as pdb:
            audit.record(
                "auth.password_reset_failed",
                "user",
                None,
                session=pdb,
                actor_type="user",
                actor_id=None,
                reason="unknown_tenant",
            )
        raise _reset_invalid()
    now = _now()
    failure: str | None = None
    user_id: uuid.UUID | None = None
    revoked = 0
    async with tenant_session(tenant.id) as db:
        reset = (
            await db.execute(
                update(PasswordReset)
                .where(
                    PasswordReset.token_hash == hash_token(data.token),
                    PasswordReset.used_at.is_(None),
                    PasswordReset.expires_at > now,
                )
                .values(used_at=now)
                .returning(PasswordReset)
                .execution_options(synchronize_session=False, populate_existing=True)
            )
        ).scalar_one_or_none()
        if reset is None:
            failure = "invalid_or_expired"
        else:
            user_id = reset.user_id
            user = await db.get(User, reset.user_id, with_for_update=True)
            if user is None or not user.is_active:
                failure = "inactive_user"  # the link is consumed either way
            else:
                audit.set_actor(db, "user", user.id)
                user.password_hash = hash_password(data.password)
                reset_login_throttle(user)
                revoked = await revoke_principal_sessions(db, "user", user.id)
        if failure is not None:
            audit.record(
                "auth.password_reset_failed",
                "user",
                user_id,
                session=db,
                actor_type="user",
                actor_id=user_id,
                reason=failure,
            )
        else:
            audit.record(
                "auth.password_reset_completed",
                "user",
                user_id,
                session=db,
                actor_type="user",
                actor_id=user_id,
                revoked_sessions=revoked,
            )
    if failure is not None:
        raise _reset_invalid()


# ================================================================ tenants
def _slug_taken() -> ConflictError:
    return ConflictError("This slug is already in use", code="slug_taken")


async def create_tenant(ctx: RequestContext, data: TenantCreate) -> Tenant:
    """Provision a tenant: tenant row (platform) -> first-admin invitation (tenant RLS session) ->
    ``TenantProvisioned`` (after commit) -> invitation email.

    The tenant must be committed before a tenant session can reference it (FK from another
    connection), so a failure while creating the invitation deletes the tenant again (compensation).
    """
    principal = require_platform(ctx)
    try:
        async with platform_session() as db:
            taken = (await db.execute(select(Tenant.id).where(Tenant.slug == data.slug))).first()
            if taken:
                raise _slug_taken()
            tenant = Tenant(
                name=data.name,
                slug=data.slug,
                timezone=data.timezone,
                currency=data.currency,
                locale=data.locale,
                seat_limit=data.seat_limit,
            )
            db.add(tenant)
            await db.flush()
            await db.refresh(tenant)
    except IntegrityError as exc:  # concurrent create with the same slug
        if violated_constraint(exc) != TENANT_SLUG_CONSTRAINT:
            raise
        raise _slug_taken() from exc

    try:
        async with tenant_session(tenant.id) as tdb:
            # Default roles are seeded in the same transaction as the invitation, which references the
            # seeded Tenant Admin role by id (see story 1.5 implementation notes).
            role_ids = await access.seed_default_roles(tdb, tenant.id)
            issued = await create_invitation(
                tdb,
                tenant_id=tenant.id,
                email=data.admin_email,
                role_ids=[role_ids[TENANT_ADMIN_ROLE]],
                invited_by_type="platform",
                invited_by_id=principal.id,
            )
            # Epic 13: registered provisioning steps (default Legal Entity + open periods, retention classes).
            from app.core.provisioning import provision_tenant

            await provision_tenant(tdb, tenant)
            audit.record("tenant.provisioned", "tenant", tenant.id, session=tdb, seat_limit=tenant.seat_limit)
            emit(tdb, TenantProvisioned(tenant_id=tenant.id, admin_email=issued.invitation.email))
    except Exception:
        logger.error("Tenant provisioning failed after tenant insert; removing tenant {}", tenant.id)
        try:
            async with platform_session() as db:
                await db.execute(delete(Tenant).where(Tenant.id == tenant.id))
        except Exception:
            # Keep the original error; the orphan tenant (no invitation) needs manual cleanup.
            logger.exception("Compensating delete failed; orphan tenant {} left behind", tenant.id)
        raise

    try:
        await send_email(tenant_admin_invitation_email(tenant, issued.invitation.email, issued.token))
    except Exception:
        # Tenant + invitation are committed; the admin can be re-invited. Never fail the provisioning.
        logger.exception("Sending the tenant admin invitation failed (tenant_id={})", tenant.id)

    logger.info("Tenant provisioned (tenant_id={}, operator_id={})", tenant.id, principal.id)
    return tenant


async def list_tenants(ctx: RequestContext, db: AsyncSession, params: PageParams) -> Page[TenantOut]:
    require_platform(ctx)
    stmt = select(Tenant).order_by(Tenant.created_at.desc(), Tenant.id)
    page = await paginate(db, stmt, params)
    return Page[TenantOut](
        items=[TenantOut.model_validate(t) for t in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


async def get_tenant(ctx: RequestContext, db: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    require_platform(ctx)
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("Tenant not found")
    return tenant


# ================================================================ story 22.9 (A10): re-login revokes the incoming session
async def revoke_incoming_session(request: Request, principal_type: PrincipalType, cookie_name: str) -> bool:
    """Revoke the session the browser presented when it signs in again (no fixation, no orphaned sessions).

    Returns True when a live session was revoked. Unknown / already-revoked tokens are ignored."""
    token = request.cookies.get(cookie_name)
    if not token:
        return False
    async with platform_session() as db:
        row = await find_session(db, principal_type, token)
        if row is None or row.revoked_at is not None:
            return False
        await revoke_session(db, row.id)
    return True
