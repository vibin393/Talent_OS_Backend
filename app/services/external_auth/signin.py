"""Client User and Vendor User sign-in (Epic 14, FR-78, FR-79, AD-10, AD-26).

Client Users: magic link + 6-digit code (one email carries both; 10 minutes, single use, hashed -- the link is
a ``secure_links`` row of purpose ``client_sign_in``; the code's SHA-256 sits in its metadata, 5 attempts).
TOTP is optional, or enforced per Client User (``mfa_enforced``).

Vendor Users: invitation (``vendor_invite`` link, 7 days) -> password (argon2id, P1 policy) -> mandatory TOTP
enrolment; then password + TOTP on every sign-in, with the P1 lockout policy. Password reset by emailed
single-use link (``vendor_password_reset``, 1 hour) revokes every session.

No session exists until every factor passed. The second factor runs behind the shared ``tos_mfa`` challenge
cookie (``mfa_challenges`` row of the principal type); enrolment challenges carry the pending secret on the
principal row. Unknown emails get the same answers and timing as known ones.

Epic 50 (FR-251 / FR-252, AD-48): ``open_client_session_via_sso`` is the only path a client IdP login uses to mint
``tos_client`` (MFA unless the IdP is ``mfa_trusted``). When the ``sso`` domain's gate (registered through
``register_client_sso_gate``) says a Client User must use SSO, a magic-link request emails the company sign-in
URL instead of a link (the response stays ``check_email``) and ``verify-link`` / ``verify-code`` answer 403
``sso_required``. Break-glass Client Users are exempt (the gate decides).
"""

import asyncio
import dataclasses
import hashlib
import hmac
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import pyotp
from fastapi import BackgroundTasks
from loguru import logger
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, rate_limit, secure_links
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import ConflictError, Forbidden, NotFound, Unauthorized, ValidationFailed
from app.core.mailer import EmailMessage, send_email
from app.core.security import (
    csrf_token_for,
    decrypt_str,
    encrypt_str,
    hash_password,
    hash_token,
    verify_password,
)
from app.core.settings import get_settings
from app.models.clients import Client
from app.models.external_auth import ClientUser, VendorUser
from app.models.identity import MfaChallenge, MfaRecoveryCode, Tenant
from app.models.vendors import Vendor
from app.schemas.external_auth_portal import (
    PortalAuthOut,
    PortalCodeVerifyIn,
    PortalIdentityOut,
    PortalInviteAcceptIn,
    PortalInviteOut,
    PortalLinkRequestIn,
    PortalMfaEnrollmentOut,
    PortalMfaIn,
    PortalPasswordLoginIn,
    PortalPasswordResetConfirmIn,
    PortalPasswordResetRequestIn,
    PortalStatusOut,
)
from app.services import auth_service, mfa_service
from app.services.external_auth import sessions

Kind = Literal["client_user", "vendor_user"]
Account = ClientUser | VendorUser

CLIENT_SIGN_IN = "client_sign_in"
VENDOR_INVITE = "vendor_invite"
VENDOR_PASSWORD_RESET = "vendor_password_reset"
LINK_TTL = timedelta(minutes=10)
CLIENT_INVITE_TTL = timedelta(days=7)
VENDOR_INVITE_TTL = timedelta(days=7)
RESET_TTL = timedelta(hours=1)
MAX_CODE_ATTEMPTS = 5
MIN_RESPONSE_SECONDS = 0.3
IP_LIMIT = 30
WINDOW = 3600
VENDOR_OK_STATUSES = frozenset({"active", "onboarding"})


def _now() -> datetime:
    return datetime.now(UTC)


def _base() -> str:
    return get_settings().frontend_url.rstrip("/")


def _code_hash(link_id: uuid.UUID, code: str) -> str:
    return hashlib.sha256(f"{link_id}:{code}".encode()).hexdigest()


def _link_invalid() -> Unauthorized:
    return Unauthorized("This sign-in link or code is invalid or has expired", code="link_invalid")


def _ip_key(ctx: RequestContext) -> str:
    return rate_limit.subject_hash(ctx.ip or "unknown")


async def _pad(started: float) -> None:
    elapsed = time.monotonic() - started
    if elapsed < MIN_RESPONSE_SECONDS:
        await asyncio.sleep(MIN_RESPONSE_SECONDS - elapsed)


# ================================================================ results
@dataclasses.dataclass(frozen=True, slots=True)
class SignInResult:
    """``auth`` for the body; ``session_token`` / ``csrf_token`` (signed in) or ``mfa_token`` (challenge)."""

    auth: PortalAuthOut
    session_token: str | None = None
    csrf_token: str | None = None
    max_age: int = 0
    mfa_token: str | None = None
    mfa_max_age: int = 0


async def identity(db: AsyncSession, kind: Kind, account: Account) -> PortalIdentityOut:
    if isinstance(account, ClientUser):
        org = (
            await db.execute(select(Client.name).where(Client.id == account.client_id))
        ).scalar_one_or_none()
        return PortalIdentityOut(
            principal_type="client_user",
            id=account.id,
            display_name=account.display_name,
            email=str(account.email),
            organization_name=str(org or ""),
            portal_permissions=sorted(account.portal_permissions or ()),
            is_admin=False,
            mfa_enabled=account.mfa_enabled_at is not None,
        )
    org = (
        await db.execute(select(Vendor.legal_name).where(Vendor.id == account.vendor_id))
    ).scalar_one_or_none()
    del kind
    return PortalIdentityOut(
        principal_type="vendor_user",
        id=account.id,
        display_name=account.display_name,
        email=str(account.email),
        organization_name=str(org or ""),
        portal_permissions=sorted(account.portal_permissions or ()),
        is_admin=account.is_vendor_admin,
        mfa_enabled=account.mfa_enabled_at is not None,
    )


async def _open_session(
    ctx: RequestContext, db: AsyncSession, kind: Kind, account: Account, method: str
) -> SignInResult:
    if account.status == "invited":
        account.status = "active"
    account.last_sign_in_at = _now()
    issued = await auth_service.create_session(
        db,
        principal_type=kind,
        principal_id=account.id,
        tenant_id=account.tenant_id,
        ip=ctx.ip,
        user_agent=ctx.user_agent,
        absolute_timeout=timedelta(hours=get_settings().external_session_absolute_hours),
    )
    await db.flush()
    audit.record(
        f"{kind}.signed_in", kind, account.id, session=db, actor_type=kind, actor_id=account.id, method=method
    )
    me = await identity(db, kind, account)
    return SignInResult(
        auth=PortalAuthOut(status="signed_in", me=me),
        session_token=issued.token,
        csrf_token=csrf_token_for(issued.token),
        max_age=issued.max_age,
    )


def _enrollment(account: Account) -> PortalMfaEnrollmentOut:
    secret = mfa_service.new_secret()
    account.mfa_secret = encrypt_str("mfa", secret)
    account.mfa_last_step = None
    uri = pyotp.TOTP(
        secret, digits=mfa_service.TOTP_DIGITS, interval=mfa_service.TOTP_INTERVAL
    ).provisioning_uri(name=str(account.email), issuer_name=mfa_service.ISSUER)
    return PortalMfaEnrollmentOut(secret=secret, otpauth_uri=uri)


async def _after_first_factor(
    ctx: RequestContext, db: AsyncSession, kind: Kind, account: Account, method: str
) -> SignInResult:
    """TOTP when enrolled; enrolment when required (always for Vendor Users); else the session."""
    enrolled = account.mfa_enabled_at is not None
    must_enroll = kind == "vendor_user" or (isinstance(account, ClientUser) and account.mfa_enforced)
    if not enrolled and not must_enroll:
        return await _open_session(ctx, db, kind, account, method)
    challenge = await auth_service.issue_mfa_challenge(db, kind, account.id, account.tenant_id)
    if enrolled:
        return SignInResult(
            auth=PortalAuthOut(status="mfa_required"),
            mfa_token=challenge.token,
            mfa_max_age=challenge.max_age,
        )
    enrollment = _enrollment(account)
    await db.flush()
    audit.record(
        f"{kind}.mfa_enrollment_started", kind, account.id, session=db, actor_type=kind, actor_id=account.id
    )
    return SignInResult(
        auth=PortalAuthOut(status="mfa_enrollment_required", enrollment=enrollment),
        mfa_token=challenge.token,
        mfa_max_age=challenge.max_age,
    )


# ================================================================ client: magic link + code
def client_sign_in_url(slug: str, token: str) -> str:
    return f"{_base()}/client/sign-in/verify?tenant={slug}#t={token}"


# ---------------------------------------------------------------- Epic 50: SSO enforcement gate
ClientSsoGate = Callable[[AsyncSession, ClientUser], Awaitable[bool]]
_client_sso_gate: ClientSsoGate | None = None


def register_client_sso_gate(gate: ClientSsoGate | None) -> None:
    """``sso`` registers ``client_login_required`` here (it sits above ``external_auth``, AD-32 layers)."""
    global _client_sso_gate
    _client_sso_gate = gate


async def _sso_required(db: AsyncSession, cu: ClientUser) -> bool:
    return _client_sso_gate is not None and await _client_sso_gate(db, cu)


def client_portal_sign_in_url(slug: str) -> str:
    """The Client Portal sign-in page (company sign-in is offered there after email discovery)."""
    return f"{_base()}/client/sign-in?tenant={slug}"


async def _refuse_if_sso_required(db: AsyncSession, cu: ClientUser) -> None:
    if not await _sso_required(db, cu):
        return
    tenant = await db.get(Tenant, cu.tenant_id)
    raise Forbidden(
        "Your organisation signs in with its company account",
        code="sso_required",
        details={"sign_in_url": client_portal_sign_in_url(tenant.slug if tenant else "")},
    )


async def _send_sso_guidance(tenant: Tenant, cu: ClientUser) -> None:
    await send_email(
        EmailMessage(
            to=str(cu.email),
            subject=f"Sign in to the {tenant.name} client portal with your company account",
            text=(
                f"Hello {cu.display_name},\n\nYour organisation signs in to the {tenant.name} client portal with "
                "its company account, so we did not send a sign-in link. Use \"Sign in with your company "
                f"account\" here:\n\n{client_portal_sign_in_url(tenant.slug)}\n\n"
                "If you cannot use your company sign-in, contact your account manager. If you did not ask to sign "
                "in, ignore this email.\n"
            ),
            tenant_id=tenant.id,
            kind="client_sso_required",
        )
    )


async def open_client_session_via_sso(
    ctx: RequestContext, db: AsyncSession, client_user: ClientUser, *, method: str, mfa_satisfied: bool
) -> "SignInResult":
    """Epic 50: the only path a client IdP login uses to mint ``tos_client`` (same session code as magic link).
    MFA is satisfied by the IdP only when it is ``mfa_trusted``; otherwise enrolled / enforced Client Users get
    the external TOTP challenge (or enrolment)."""
    if client_user.status not in ("active", "invited"):
        raise Forbidden("Your client portal access is suspended or revoked", code="client_user_inactive")
    if mfa_satisfied:
        return await _open_session(ctx, db, "client_user", client_user, method)
    return await _after_first_factor(ctx, db, "client_user", client_user, method)


async def client_sso_mfa_enrollment(token: str | None) -> PortalAuthOut:
    """Epic 50: after an SSO redirect to the TOTP step, return the enrolment (secret + otpauth URI) of a
    Client User who must enrol (``mfa_enforced``) -- the redirect cannot carry it. Enrolled users get
    ``mfa_required``."""
    if not token:
        raise _challenge_expired()
    async with platform_session() as pdb:
        found = (
            await pdb.execute(
                select(MfaChallenge).where(
                    MfaChallenge.token_hash == hash_token(token), MfaChallenge.principal_type == "client_user"
                )
            )
        ).scalar_one_or_none()
        challenge_id = found.id if found else None
        tenant_id = found.tenant_id if found else None
    if challenge_id is None or tenant_id is None or await auth_service.active_tenant(tenant_id) is None:
        raise _challenge_expired()
    now = _now()
    async with tenant_session(tenant_id) as db:
        challenge = await db.get(MfaChallenge, challenge_id, with_for_update=True, populate_existing=True)
        account = await _load_account(db, "client_user", challenge.principal_id) if challenge else None
        if (
            challenge is None
            or account is None
            or challenge.consumed_at is not None
            or challenge.expires_at <= now
            or account.status not in ("active", "invited")
        ):
            raise _challenge_expired()
        if account.mfa_enabled_at is not None:
            return PortalAuthOut(status="mfa_required")
        enrollment = _enrollment(account)
        await db.flush()
        audit.record(
            "client_user.mfa_enrollment_started",
            "client_user",
            account.id,
            session=db,
            actor_type="client_user",
            actor_id=account.id,
        )
        return PortalAuthOut(status="mfa_enrollment_required", enrollment=enrollment)


async def issue_client_link(
    db: AsyncSession, tenant: Tenant, cu: ClientUser, ttl: timedelta, kind: str
) -> None:
    """Issue a link + code (revoking older open ones) and email them."""
    await secure_links.revoke_for_subject(db, "client_user", cu.id, CLIENT_SIGN_IN)
    code = f"{secrets.randbelow(1_000_000):06d}"
    link, token = await secure_links.issue(
        db, tenant_id=tenant.id, purpose=CLIENT_SIGN_IN, subject_type="client_user", subject_id=cu.id, ttl=ttl
    )
    link.meta = {"code_hash": _code_hash(link.id, code), "attempts": 0}
    await db.flush()
    minutes = int(ttl.total_seconds() // 60)
    lifetime = f"{minutes} minutes" if minutes < 120 else f"{minutes // 1440} days"
    intro = (
        f"You have been given access to the {tenant.name} client portal."
        if kind == "client_portal_invitation"
        else f"Use this link to sign in to the {tenant.name} client portal."
    )
    await send_email(
        EmailMessage(
            to=str(cu.email),
            subject=f"Your {tenant.name} client portal sign-in",
            text=(
                f"Hello {cu.display_name},\n\n{intro}\n\n{client_sign_in_url(tenant.slug, token)}\n\n"
                f"Or enter this code on the sign-in page: {code}\n\n"
                f"The link and code work once and expire in {lifetime}. If you did not ask for them, ignore this email.\n"  # ruff: ignore[line-too-long]
            ),
            tenant_id=tenant.id,
            kind=kind,
        )
    )


async def _client_for_email(db: AsyncSession, email: str) -> ClientUser | None:
    rows = (
        await db.execute(
            select(ClientUser)
            .where(ClientUser.email == email, ClientUser.status.in_(("active", "invited")))
            .order_by(ClientUser.last_sign_in_at.desc().nulls_last(), ClientUser.created_at.desc())
        )
    ).scalars()
    return next(iter(rows), None)


async def request_client_link(
    ctx: RequestContext, data: PortalLinkRequestIn, background: BackgroundTasks
) -> PortalStatusOut:
    started = time.monotonic()
    await rate_limit.enforce("client_link_ip", _ip_key(ctx), IP_LIMIT, WINDOW)
    background.add_task(_send_client_link, data.tenant_slug.strip().lower(), str(data.email).strip().lower())
    await _pad(started)
    return PortalStatusOut(status="check_email")


async def _send_client_link(slug: str, email: str) -> None:
    try:
        tenant = await auth_service.tenant_by_slug(slug)
        if tenant is None:
            return
        async with tenant_session(tenant.id) as db:
            audit.set_actor(db, "system", None)
            cu = await _client_for_email(db, email)
            outcome = "unknown_email" if cu is None else "issued"
            if cu is not None and await _sso_required(db, cu):
                # Epic 50: no link; the address owner learns to use company sign-in (uniform response).
                outcome = "sso_required"
                await _send_sso_guidance(tenant, cu)
            elif cu is not None:
                await issue_client_link(db, tenant, cu, LINK_TTL, "client_sign_in")
            audit.record(
                "client_user.link_requested",
                "client_user",
                cu.id if cu else None,
                session=db,
                outcome=outcome,
            )
    except Exception:
        logger.exception("Client sign-in link request failed")


async def _client_from_link(db: AsyncSession, subject_id: uuid.UUID) -> ClientUser:
    cu = (
        await db.execute(select(ClientUser).where(ClientUser.id == subject_id).with_for_update())
    ).scalar_one_or_none()
    if cu is None or cu.status not in ("active", "invited"):
        raise _link_invalid()
    return cu


async def verify_client_link(ctx: RequestContext, token: str) -> SignInResult:
    try:
        link = await secure_links.resolve(token, (CLIENT_SIGN_IN,))
    except (NotFound, secure_links.LinkGone) as exc:
        raise _link_invalid() from exc
    async with tenant_session(link.tenant_id) as db:
        audit.set_actor(db, "client_user", link.subject_id)
        await secure_links.consume(db, link.id)
        cu = await _client_from_link(db, link.subject_id)
        await _refuse_if_sso_required(db, cu)
        return await _after_first_factor(ctx, db, "client_user", cu, "magic_link")


async def verify_client_code(ctx: RequestContext, data: PortalCodeVerifyIn) -> SignInResult:
    started = time.monotonic()
    await rate_limit.enforce("client_code_ip", _ip_key(ctx), IP_LIMIT, WINDOW)
    tenant = await auth_service.tenant_by_slug(data.tenant_slug)
    if tenant is None:
        await _pad(started)
        raise _link_invalid()
    result: SignInResult | None = None
    async with tenant_session(tenant.id) as db:
        cu = await _client_for_email(db, str(data.email).lower())
        link = None
        if cu is not None:
            links = await secure_links.links_for_subject(db, "client_user", cu.id)
            now = _now()
            link = next(
                (
                    lk
                    for lk in links
                    if lk.purpose == CLIENT_SIGN_IN
                    and lk.revoked_at is None
                    and lk.consumed_at is None
                    and lk.expires_at > now
                ),
                None,
            )
        if cu is not None and link is not None:
            meta = dict(link.meta or {})
            attempts = int(meta.get("attempts", 0))
            ok = attempts < MAX_CODE_ATTEMPTS and hmac.compare_digest(
                str(meta.get("code_hash", "")), _code_hash(link.id, data.code)
            )
            if ok:
                audit.set_actor(db, "client_user", cu.id)
                await secure_links.consume(db, link.id)
                locked = await _client_from_link(db, cu.id)
                await _refuse_if_sso_required(db, locked)
                result = await _after_first_factor(ctx, db, "client_user", locked, "code")
            else:
                meta["attempts"] = attempts + 1
                link.meta = meta
                if attempts + 1 >= MAX_CODE_ATTEMPTS:
                    link.revoked_at = now
                await db.flush()
    if result is None:
        await _pad(started)
        raise _link_invalid()
    return result


# ================================================================ vendor: invitation, password, reset
def vendor_invite_url(slug: str, token: str) -> str:
    return f"{_base()}/vendor/accept-invite?tenant={slug}#t={token}"


def vendor_reset_url(slug: str, token: str) -> str:
    return f"{_base()}/vendor/reset-password?tenant={slug}#t={token}"


async def send_vendor_invitation(db: AsyncSession, tenant: Tenant, vu: VendorUser) -> None:
    await secure_links.revoke_for_subject(db, "vendor_user", vu.id, VENDOR_INVITE)
    _, token = await secure_links.issue(
        db,
        tenant_id=tenant.id,
        purpose=VENDOR_INVITE,
        subject_type="vendor_user",
        subject_id=vu.id,
        ttl=VENDOR_INVITE_TTL,
    )
    await send_email(
        EmailMessage(
            to=str(vu.email),
            subject=f"You are invited to the {tenant.name} vendor portal",
            text=(
                f"Hello {vu.display_name},\n\nYou have been invited to the {tenant.name} vendor portal. Set your "  # ruff: ignore[line-too-long]
                f"password and authenticator app here:\n{vendor_invite_url(tenant.slug, token)}\n\n"
                "The invitation expires in 7 days.\n"
            ),
            tenant_id=tenant.id,
            kind="vendor_portal_invitation",
        )
    )


async def lookup_vendor_invite(token: str) -> PortalInviteOut:
    try:
        link = await secure_links.resolve(token, (VENDOR_INVITE,))
    except (NotFound, secure_links.LinkGone) as exc:
        raise _link_invalid() from exc
    async with tenant_session(link.tenant_id) as db:
        vu = (
            await db.execute(select(VendorUser).where(VendorUser.id == link.subject_id))
        ).scalar_one_or_none()
        tenant = await db.get(Tenant, link.tenant_id)
        if vu is None or vu.status != "invited" or tenant is None:
            raise _link_invalid()
        org = (
            await db.execute(select(Vendor.legal_name).where(Vendor.id == vu.vendor_id))
        ).scalar_one_or_none()
        return PortalInviteOut(
            email=str(vu.email),
            display_name=vu.display_name,
            organization_name=str(org or ""),
            tenant_name=tenant.name,
        )


async def accept_vendor_invite(ctx: RequestContext, data: PortalInviteAcceptIn) -> SignInResult:
    auth_service.validate_password_strength(data.password)
    try:
        link = await secure_links.resolve(data.token, (VENDOR_INVITE,))
    except (NotFound, secure_links.LinkGone) as exc:
        raise _link_invalid() from exc
    async with tenant_session(link.tenant_id) as db:
        audit.set_actor(db, "vendor_user", link.subject_id)
        await secure_links.consume(db, link.id)
        vu = (
            await db.execute(select(VendorUser).where(VendorUser.id == link.subject_id).with_for_update())
        ).scalar_one_or_none()
        if vu is None or vu.status != "invited" or not await _vendor_ok(db, vu):
            raise _link_invalid()
        vu.password_hash = hash_password(data.password)
        vu.password_changed_at = _now()
        if data.display_name:
            vu.display_name = data.display_name
        audit.record("vendor_user.invitation_accepted", "vendor_user", vu.id, session=db)
        # Mandatory TOTP: the account becomes active once enrolment is confirmed.
        return await _after_first_factor(ctx, db, "vendor_user", vu, "invitation")


async def _vendor_ok(db: AsyncSession, vu: VendorUser) -> bool:
    status = (await db.execute(select(Vendor.status).where(Vendor.id == vu.vendor_id))).scalar_one_or_none()
    return status in VENDOR_OK_STATUSES


async def vendor_login(ctx: RequestContext, data: PortalPasswordLoginIn) -> SignInResult:
    await rate_limit.enforce("vendor_login_ip", _ip_key(ctx), IP_LIMIT, WINDOW)
    tenant = await auth_service.tenant_by_slug(data.tenant_slug)
    if tenant is None:
        verify_password(None, data.password)
        raise auth_service.invalid_credentials()
    now = _now()
    error: Unauthorized | None = None
    result: SignInResult | None = None
    async with tenant_session(tenant.id) as db:
        vu = (
            await db.execute(
                select(VendorUser).where(VendorUser.email == str(data.email).lower()).with_for_update()
            )
        ).scalar_one_or_none()
        audit.set_actor(db, "vendor_user", vu.id if vu else None)
        # An invited user who set a password but has not finished TOTP enrolment may resume it.
        usable = (
            vu is not None
            and vu.status in ("active", "invited")
            and vu.password_hash is not None
            and await _vendor_ok(db, vu)
        )
        check = auth_service.check_login_attempt(
            vu,  # type: ignore[arg-type]
            password_hash=vu.password_hash if vu else None,
            is_active=usable,
            password=data.password,
            now=now,
        )
        if vu is None or check.outcome != "ok":
            error = check.error()
            if vu is not None:
                audit.record(
                    "vendor_user.login_failed", "vendor_user", vu.id, session=db, reason=check.outcome
                )
        else:
            result = await _after_first_factor(ctx, db, "vendor_user", vu, "password")
    if error is not None or result is None:
        raise error or auth_service.invalid_credentials()
    return result


async def request_vendor_reset(
    ctx: RequestContext, data: PortalPasswordResetRequestIn, background: BackgroundTasks
) -> PortalStatusOut:
    started = time.monotonic()
    await rate_limit.enforce("vendor_reset_ip", _ip_key(ctx), IP_LIMIT, WINDOW)
    background.add_task(_send_vendor_reset, data.tenant_slug.strip().lower(), str(data.email).strip().lower())
    await _pad(started)
    return PortalStatusOut(status="check_email")


async def _send_vendor_reset(slug: str, email: str) -> None:
    try:
        tenant = await auth_service.tenant_by_slug(slug)
        if tenant is None:
            return
        async with tenant_session(tenant.id) as db:
            audit.set_actor(db, "system", None)
            vu = (await db.execute(select(VendorUser).where(VendorUser.email == email))).scalar_one_or_none()
            if vu is None or vu.status != "active":
                return
            await secure_links.revoke_for_subject(db, "vendor_user", vu.id, VENDOR_PASSWORD_RESET)
            _, token = await secure_links.issue(
                db,
                tenant_id=tenant.id,
                purpose=VENDOR_PASSWORD_RESET,
                subject_type="vendor_user",
                subject_id=vu.id,
                ttl=RESET_TTL,
            )
            audit.record("vendor_user.password_reset_requested", "vendor_user", vu.id, session=db)
            await send_email(
                EmailMessage(
                    to=str(vu.email),
                    subject=f"Reset your {tenant.name} vendor portal password",
                    text=(
                        f"Hello {vu.display_name},\n\nReset your password here (valid for 1 hour, single use):\n"  # ruff: ignore[line-too-long]
                        f"{vendor_reset_url(tenant.slug, token)}\n\nIf you did not ask for this, ignore this email.\n"  # ruff: ignore[line-too-long]
                    ),
                    tenant_id=tenant.id,
                    kind="vendor_password_reset",
                )
            )
    except Exception:
        logger.exception("Vendor password reset request failed")


async def confirm_vendor_reset(ctx: RequestContext, data: PortalPasswordResetConfirmIn) -> PortalStatusOut:  # ruff: ignore[unused-function-argument]
    auth_service.validate_password_strength(data.password)
    try:
        link = await secure_links.resolve(data.token, (VENDOR_PASSWORD_RESET,))
    except (NotFound, secure_links.LinkGone) as exc:
        raise _link_invalid() from exc
    async with tenant_session(link.tenant_id) as db:
        audit.set_actor(db, "vendor_user", link.subject_id)
        await secure_links.consume(db, link.id)
        vu = (
            await db.execute(select(VendorUser).where(VendorUser.id == link.subject_id).with_for_update())
        ).scalar_one_or_none()
        if vu is None or vu.status != "active":
            raise _link_invalid()
        vu.password_hash = hash_password(data.password)
        vu.password_changed_at = _now()
        auth_service.reset_login_throttle(vu)  # type: ignore[arg-type]
        await sessions.revoke_all(db, "vendor_user", vu.id)
        audit.record("vendor_user.password_reset", "vendor_user", vu.id, session=db)
    return PortalStatusOut(status="ok")


# ================================================================ second factor (both kinds)
async def _load_account(db: AsyncSession, kind: Kind, principal_id: uuid.UUID) -> Account | None:
    model = ClientUser if kind == "client_user" else VendorUser
    return (  # type: ignore[return-value]
        await db.execute(select(model).where(model.id == principal_id).with_for_update())
    ).scalar_one_or_none()


def _check_totp(account: Account, code: str, now: datetime) -> bool:
    if not account.mfa_secret:
        return False
    secret = decrypt_str("mfa", account.mfa_secret)
    step = mfa_service.matching_step(secret, code, last_step=account.mfa_last_step, now=now)
    if step is None:
        return False
    account.mfa_last_step = step
    return True


async def _use_recovery_code(
    db: AsyncSession, kind: Kind, account: Account, code: str, now: datetime
) -> bool:
    row = (
        await db.execute(
            update(MfaRecoveryCode)
            .where(
                MfaRecoveryCode.principal_type == kind,
                MfaRecoveryCode.principal_id == account.id,
                MfaRecoveryCode.code_hash == mfa_service.hash_recovery_code(code),
                MfaRecoveryCode.used_at.is_(None),
            )
            .values(used_at=now)
            .returning(MfaRecoveryCode.id)
        )
    ).first()
    return row is not None


async def _replace_recovery_codes(db: AsyncSession, kind: Kind, account: Account) -> list[str]:
    await db.execute(
        delete(MfaRecoveryCode).where(
            MfaRecoveryCode.principal_type == kind, MfaRecoveryCode.principal_id == account.id
        )
    )
    codes = [mfa_service.new_recovery_code() for _ in range(mfa_service.RECOVERY_CODE_COUNT)]
    db.add_all(
        MfaRecoveryCode(
            tenant_id=account.tenant_id,
            principal_type=kind,
            principal_id=account.id,
            code_hash=mfa_service.hash_recovery_code(c),
        )
        for c in codes
    )
    await db.flush()
    return codes


def _challenge_expired() -> Unauthorized:
    return Unauthorized("The sign-in attempt expired; start again", code="mfa_challenge_expired")


async def verify_mfa(ctx: RequestContext, kind: Kind, token: str | None, body: PortalMfaIn) -> SignInResult:
    """Second factor: a TOTP / recovery code for enrolled accounts, or the first TOTP code confirming
    enrolment (which also returns the recovery codes, shown once)."""
    if not token:
        raise _challenge_expired()
    async with platform_session() as pdb:
        found = (
            await pdb.execute(
                select(MfaChallenge).where(
                    MfaChallenge.token_hash == hash_token(token), MfaChallenge.principal_type == kind
                )
            )
        ).scalar_one_or_none()
        challenge_id = found.id if found else None
        tenant_id = found.tenant_id if found else None
    if challenge_id is None or tenant_id is None or await auth_service.active_tenant(tenant_id) is None:
        raise _challenge_expired()
    now = _now()
    error: Unauthorized | ValidationFailed | None = None
    result: SignInResult | None = None
    async with tenant_session(tenant_id) as db:
        challenge = await db.get(MfaChallenge, challenge_id, with_for_update=True, populate_existing=True)
        account = await _load_account(db, kind, challenge.principal_id) if challenge else None
        usable = (
            challenge is not None
            and challenge.consumed_at is None
            and challenge.expires_at > now
            and challenge.attempts < mfa_service.MAX_CHALLENGE_ATTEMPTS
        )
        blocked = (
            not usable
            or account is None
            or account.status not in ("active", "invited")
            or (isinstance(account, VendorUser) and not await _vendor_ok(db, account))
        )
        if blocked or account is None:
            error = _challenge_expired()
        else:
            assert challenge is not None
            audit.set_actor(db, kind, account.id)
            enrolled = account.mfa_enabled_at is not None
            ok = False
            method = "totp"
            if body.code is not None:
                ok = _check_totp(account, body.code, now)
            elif enrolled and body.recovery_code is not None:
                ok = await _use_recovery_code(db, kind, account, body.recovery_code, now)
                method = "recovery_code"
            if not ok:
                challenge.attempts += 1
                if challenge.attempts >= mfa_service.MAX_CHALLENGE_ATTEMPTS:
                    challenge.consumed_at = now
                audit.record(f"{kind}.mfa_failed", kind, account.id, session=db)
                error = Unauthorized("The code is invalid or was already used", code="invalid_code")
            else:
                challenge.consumed_at = now
                codes: list[str] = []
                if not enrolled:
                    account.mfa_enabled_at = now
                    codes = await _replace_recovery_codes(db, kind, account)
                    audit.record(f"{kind}.mfa_enrolled", kind, account.id, session=db)
                signed_in = await _open_session(ctx, db, kind, account, f"mfa_{method}")
                result = dataclasses.replace(
                    signed_in, auth=signed_in.auth.model_copy(update={"recovery_codes": codes})
                )
    if error is not None or result is None:
        raise error or _challenge_expired()
    return result


async def reset_mfa(db: AsyncSession, kind: Kind, account: Account) -> None:
    """Admin reset (internal staff or a vendor admin): next sign-in re-enrols; sessions revoked."""
    account.mfa_secret = None
    account.mfa_enabled_at = None
    account.mfa_last_step = None
    await db.execute(
        delete(MfaRecoveryCode).where(
            MfaRecoveryCode.principal_type == kind, MfaRecoveryCode.principal_id == account.id
        )
    )
    await sessions.revoke_all(db, kind, account.id)
    await db.flush()
    audit.record(f"{kind}.mfa_reset", kind, account.id, session=db)


async def logout(ctx: RequestContext) -> None:
    principal = ctx.principal
    if principal is None or principal.session_id is None or principal.tenant_id is None:
        return
    async with tenant_session(principal.tenant_id) as db:
        await auth_service.revoke_session(db, principal.session_id)


def conflict_last_admin() -> ConflictError:
    return ConflictError("The vendor must keep at least one active admin", code="last_vendor_admin")
