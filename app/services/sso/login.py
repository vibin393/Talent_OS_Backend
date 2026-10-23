"""SSO sign-in (Epic 14, FR-77, AD-30).

* ``discover``: email-domain match only, identical shape for unknown emails; per-IP rate limited.
* ``start``: stores a single-use login state (``sso_login_states``: hash of ``state`` -> tenant, IdP, nonce /
  AuthnRequest id, sealed PKCE verifier, ``next``), 10-minute TTL, then redirects to the IdP.
* ``complete_oidc`` / ``complete_saml``: validate (state single use, nonce, iss / aud / exp,
  ``email_verified`` for email linking; SAML signature, audience, recipient, NotOnOrAfter, InResponseTo), then
  resolve the User: identity link -> verified-email link (audited ``sso.identity_linked``) -> JIT (seat
  limit, default Role, group mapping) -> refuse. Deactivated Users are refused. Then the same session path as
  password sign-in (``auth_service.open_user_sign_in``); MFA is satisfied by the IdP only when the IdP is
  ``mfa_trusted``, else the P1 challenge follows.
* Test logins (``is_test`` state) resolve but create nothing and return the mapped claims.
* Epic 50 (AD-48): the IdP row decides the principal type. Internal discovery / start only see
  ``audience='internal'`` IdPs; the Client Portal uses ``start(..., audience="client")``. The shared callbacks
  branch in ``_finish``: a client IdP resolves only ``client_users`` of ``idp.client_id`` (``sso.client``) and
  the session is opened by ``external_auth`` (``tos_client``). Errors after the IdP is known carry
  ``sso_audience`` so the router redirects to the right sign-in page.
"""

import dataclasses
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, rate_limit
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import DomainError, Forbidden, NotFound
from app.core.events import emit
from app.core.security import decrypt_str, encrypt_str, hash_password, new_token
from app.events.sso import UserJitProvisioned, UserLinkedToIdentity
from app.models.identity import Role, Tenant, User
from app.models.sso import IdentityProvider, IdentityProviderGroupRole, SsoLoginState, UserIdentity
from app.schemas.client_sso import ClientSsoTestResultOut
from app.schemas.sso import SsoDiscoverIn, SsoDiscoverOut, SsoTestResultOut
from app.services import access_service as access
from app.services import auth_service
from app.services.sso import oidc, saml
from app.services.sso.service import acs_url, redirect_uri, sp_entity_id

STATE_TTL = timedelta(minutes=10)
DISCOVER_IP_LIMIT = 30
START_IP_LIMIT = 30
WINDOW_SECONDS = 600


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def safe_next(value: str | None) -> str:
    r"""Same-origin relative paths only (``/x``; never ``//host`` or ``/\\host``)."""
    if not value or not value.startswith("/") or value.startswith("//") or value.startswith("/\\"):
        return "/dashboard"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/dashboard"
    return value[:500]


def safe_client_next(value: str | None) -> str:
    """Client Portal relay: ``/client`` paths only (default ``/client``)."""
    path = safe_next(value)
    return path if path == "/client" or path.startswith(("/client/", "/client?")) else "/client"


def tag_audience(exc: DomainError, audience: str) -> DomainError:
    """Mark a sign-in failure with the IdP audience (the router picks the sign-in page from it)."""
    exc.sso_audience = audience  # type: ignore[attr-defined]
    return exc


def failure_audience(exc: Exception) -> str:
    return str(getattr(exc, "sso_audience", "internal"))


def _ip_key(ctx: RequestContext) -> str:
    return rate_limit.subject_hash(ctx.ip or "unknown")


# ================================================================ discover
async def discover(ctx: RequestContext, data: SsoDiscoverIn) -> SsoDiscoverOut:
    await rate_limit.enforce("sso_discover_ip", _ip_key(ctx), DISCOVER_IP_LIMIT, WINDOW_SECONDS)
    tenant = await auth_service.tenant_by_slug(data.tenant_slug)
    if tenant is None:
        return SsoDiscoverOut(sso=False)
    domain = str(data.email).rsplit("@", 1)[-1].lower()
    async with tenant_session(tenant.id) as db:
        rows = (
            await db.execute(
                select(IdentityProvider).where(
                    IdentityProvider.status == "active", IdentityProvider.audience == "internal"
                )
            )
        ).scalars()
        match = next((r for r in rows if domain in (r.email_domains or [])), None)
        if match is None:
            return SsoDiscoverOut(sso=False)
        return SsoDiscoverOut(sso=True, idp_id=match.id, required=match.enforce_for == "all")


# ================================================================ start
async def start(
    ctx: RequestContext,
    idp_id: uuid.UUID,
    tenant_slug: str,
    next_path: str | None,
    *,
    test: bool,
    audience: str = "internal",
) -> str:
    await rate_limit.enforce("sso_start_ip", _ip_key(ctx), START_IP_LIMIT, WINDOW_SECONDS)
    tenant = await auth_service.tenant_by_slug(tenant_slug)
    if tenant is None:
        raise NotFound("Identity provider not found")
    async with tenant_session(tenant.id) as db:
        idp = (
            await db.execute(
                select(IdentityProvider).where(
                    IdentityProvider.id == idp_id, IdentityProvider.audience == audience
                )
            )
        ).scalar_one_or_none()
        # Test logins run against draft providers too (the admin validates before activating).
        if idp is None or (idp.status != "active" and not (test and idp.status == "draft")):
            raise NotFound("Identity provider not found")
        kind, config = idp.kind, dict(idp.config or {})
    state = oidc.new_token()
    row = SsoLoginState(
        state_hash=_hash(state),
        tenant_id=tenant.id,
        idp_id=idp_id,
        nonce="",
        relay_to=safe_client_next(next_path) if audience == "client" else safe_next(next_path),
        is_test=test,
        expires_at=_now() + STATE_TTL,
    )
    if kind == "oidc":
        nonce, verifier = oidc.new_token(), oidc.new_token(64)
        meta = await oidc.discovery(config)
        url = oidc.authorize_url(
            meta, config, redirect_uri=redirect_uri(), state=state, nonce=nonce, verifier=verifier
        )
        row.nonce = nonce
        row.pkce_verifier = encrypt_str("sso", verifier)
    else:
        url, request_id = saml.login_url(
            config, sp_entity_id=sp_entity_id(idp_id), acs_url=acs_url(idp_id), relay_state=state
        )
        row.nonce = request_id
    async with platform_session() as pdb:
        pdb.add(row)
    return url


# ================================================================ complete
@dataclasses.dataclass(frozen=True, slots=True)
class Claims:
    subject: str
    email: str | None
    email_verified: bool
    name: str | None
    groups: list[str]


@dataclasses.dataclass(frozen=True, slots=True)
class LoginResult:
    """One of: a signed-in session, an MFA challenge, or a test-login report.

    Epic 50: ``client`` is the Client Portal result (``tos_client`` session or external MFA challenge) of a
    client IdP (``external_auth.signin.SignInResult``); ``auth`` / ``mfa`` are never set on that path."""

    next_path: str
    auth: auth_service.UserAuthResult | None = None
    mfa: auth_service.MfaChallengeResult | None = None
    test: SsoTestResultOut | ClientSsoTestResultOut | None = None
    client: Any = None
    audience: str = "internal"


async def _consume_state(state: str | None, *, idp_id: uuid.UUID | None = None) -> SsoLoginState:
    """Single use: exactly one of two racing callbacks wins; replays and expired states are refused."""
    if not state:
        raise oidc.invalid("state")
    now = _now()
    async with platform_session() as pdb:
        row = (
            await pdb.execute(
                update(SsoLoginState)
                .where(
                    SsoLoginState.state_hash == _hash(state),
                    SsoLoginState.used_at.is_(None),
                    SsoLoginState.expires_at > now,
                )
                .values(used_at=now)
                .returning(SsoLoginState)
            )
        ).scalar_one_or_none()
        if row is not None:
            pdb.expunge(row)
    if row is None or (idp_id is not None and row.idp_id != idp_id):
        raise oidc.invalid("state")
    return row


async def complete_oidc(ctx: RequestContext, code: str | None, state: str | None) -> LoginResult:
    login_state = await _consume_state(state)
    async with tenant_session(login_state.tenant_id) as db:
        idp = await _idp(db, login_state.idp_id)
        audience = idp.audience
        if idp.kind != "oidc" or not code:
            raise tag_audience(oidc.invalid("state"), audience)
        config, secret = dict(idp.config or {}), decrypt_str("sso", idp.secret) if idp.secret else None
    try:
        return await _complete_oidc(ctx, login_state, idp, config, secret, code)
    except DomainError as exc:
        raise tag_audience(exc, audience) from None


async def _complete_oidc(
    ctx: RequestContext,
    login_state: SsoLoginState,
    idp: IdentityProvider,
    config: dict[str, Any],
    secret: str | None,
    code: str,
) -> LoginResult:
    meta = await oidc.discovery(config)
    verifier = decrypt_str("sso", login_state.pkce_verifier or "")
    raw = await oidc.exchange_code(
        meta,
        config,
        client_secret=secret,
        code=code,
        verifier=verifier,
        redirect_uri=redirect_uri(),
        nonce=login_state.nonce,
    )
    group_claim = idp.group_claim
    groups_raw = raw.get(group_claim) if group_claim else None
    claims = Claims(
        subject=str(raw["sub"]),
        email=str(raw["email"]).lower() if raw.get("email") else None,
        email_verified=raw.get("email_verified") is True,
        name=str(raw.get("name")) if raw.get("name") else None,
        groups=[str(g) for g in groups_raw] if isinstance(groups_raw, list) else [],
    )
    return await _finish(ctx, login_state, claims)


async def complete_saml(
    ctx: RequestContext, idp_id: uuid.UUID, saml_response: str | None, relay_state: str | None
) -> LoginResult:
    login_state = await _consume_state(relay_state, idp_id=idp_id)
    if not saml_response:
        raise saml.invalid("missing")
    async with tenant_session(login_state.tenant_id) as db:
        idp = await _idp(db, idp_id)
        audience = idp.audience
        if idp.kind != "saml":
            raise tag_audience(saml.invalid("state"), audience)
        config, group_claim = dict(idp.config or {}), idp.group_claim
    try:
        return await _complete_saml(ctx, login_state, config, group_claim, idp_id, saml_response, relay_state)
    except DomainError as exc:
        raise tag_audience(exc, audience) from None


async def _complete_saml(
    ctx: RequestContext,
    login_state: SsoLoginState,
    config: dict[str, Any],
    group_claim: str | None,
    idp_id: uuid.UUID,
    saml_response: str,
    relay_state: str | None,
) -> LoginResult:
    identity = saml.process_response(
        config,
        sp_entity_id=sp_entity_id(idp_id),
        acs_url=acs_url(idp_id),
        saml_response=saml_response,
        relay_state=relay_state or "",
        request_id=login_state.nonce,
        group_attribute=group_claim,
    )
    # A signed assertion from the configured IdP vouches for the email it carries.
    claims = Claims(
        subject=identity.subject,
        email=identity.email.lower() if identity.email else None,
        email_verified=identity.email is not None,
        name=identity.name,
        groups=identity.groups,
    )
    return await _finish(ctx, login_state, claims)


async def _idp(db: AsyncSession, idp_id: uuid.UUID) -> IdentityProvider:
    idp = (
        await db.execute(select(IdentityProvider).where(IdentityProvider.id == idp_id))
    ).scalar_one_or_none()
    if idp is None or idp.status == "disabled":
        raise oidc.invalid("identity_provider")
    return idp


def _deactivated() -> Forbidden:
    return Forbidden("This account is deactivated", code="user_deactivated")


async def _finish(ctx: RequestContext, login_state: SsoLoginState, claims: Claims) -> LoginResult:
    tenant = await auth_service.active_tenant(login_state.tenant_id)
    if tenant is None:
        raise oidc.invalid("tenant")
    async with tenant_session(tenant.id) as db:
        audience = (await _idp(db, login_state.idp_id)).audience
    if audience == "client":
        from app.services.sso import client

        return await client.finish(ctx, tenant, login_state, claims)
    next_path = login_state.relay_to or "/dashboard"
    if login_state.is_test:
        async with tenant_session(tenant.id) as db:
            idp = await _idp(db, login_state.idp_id)
            resolution = await _resolution(db, idp, claims)
            audit.record("sso.test_login", "identity_provider", idp.id, session=db, resolution=resolution)
        return LoginResult(
            next_path=next_path,
            test=SsoTestResultOut(
                subject=claims.subject,
                email=claims.email,
                email_verified=claims.email_verified,
                name=claims.name,
                groups=claims.groups,
                resolution=resolution,
            ),
        )
    error: DomainError | None = None
    result: LoginResult | None = None
    async with tenant_session(tenant.id) as db:
        audit.set_actor(db, "user", None)
        idp = await _idp(db, login_state.idp_id)
        try:
            user = await _resolve_user(db, tenant, idp, claims)
        except DomainError as exc:
            error = exc
            audit.record("sso.login_refused", "identity_provider", idp.id, session=db, reason=exc.code)
        else:
            audit.set_actor(db, "user", user.id)
            now = _now()
            await db.execute(
                update(UserIdentity)
                .where(UserIdentity.idp_id == idp.id, UserIdentity.user_id == user.id)
                .values(last_login_at=now)
            )
            if user.mfa_enabled and not idp.mfa_trusted:
                challenge = await auth_service.issue_mfa_challenge(db, "user", user.id, tenant.id)
                audit.record("auth.mfa_challenge_issued", "user", user.id, session=db, method="sso")
                result = LoginResult(next_path=next_path, mfa=challenge)
            else:
                signed_in = await auth_service.open_user_sign_in(
                    ctx, db, user, tenant, now, method=f"sso_{idp.kind}"
                )
                result = LoginResult(next_path=next_path, auth=signed_in)
    if error is not None:
        raise error
    assert result is not None
    return result


async def _linked_user(db: AsyncSession, idp: IdentityProvider, subject: str) -> User | None:
    link = (
        await db.execute(
            select(UserIdentity).where(UserIdentity.idp_id == idp.id, UserIdentity.subject == subject)
        )
    ).scalar_one_or_none()
    return await db.get(User, link.user_id) if link is not None else None


async def _resolution(db: AsyncSession, idp: IdentityProvider, claims: Claims) -> str:
    if await _linked_user(db, idp, claims.subject) is not None:
        return "linked"
    if claims.email and claims.email_verified:
        if (await db.execute(select(User.id).where(User.email == claims.email))).first() is not None:
            return "link_by_email"
        if idp.jit_enabled:
            return "jit"
    return "refused"


async def _resolve_user(db: AsyncSession, tenant: Tenant, idp: IdentityProvider, claims: Claims) -> User:
    user = await _linked_user(db, idp, claims.subject)
    if user is not None:
        if not user.is_active:
            raise _deactivated()
        return user
    if not claims.email or not claims.email_verified:
        raise Forbidden(
            "Your identity provider did not confirm your email address", code="sso_email_unverified"
        )
    user = (
        await db.execute(select(User).where(User.email == claims.email).with_for_update())
    ).scalar_one_or_none()
    if user is not None:
        if not user.is_active:
            raise _deactivated()
        if (
            await db.execute(
                select(UserIdentity.id).where(UserIdentity.idp_id == idp.id, UserIdentity.user_id == user.id)
            )
        ).first() is not None:
            # The user is already linked to another subject of this IdP: never re-link silently.
            raise Forbidden("This account is linked to another identity", code="sso_identity_conflict")
        await _link(db, idp, user, claims, method="verified_email")
        return user
    if not idp.jit_enabled:
        raise Forbidden("No account exists for this email address", code="sso_no_account")
    return await _jit(db, tenant, idp, claims)


async def _link(db: AsyncSession, idp: IdentityProvider, user: User, claims: Claims, *, method: str) -> None:
    db.add(
        UserIdentity(
            id=uuid.uuid4(),
            tenant_id=user.tenant_id,
            user_id=user.id,
            idp_id=idp.id,
            subject=claims.subject[:255],
            email_at_link=claims.email,
        )
    )
    await db.flush()
    audit.record("sso.identity_linked", "user", user.id, session=db, idp_id=idp.id, method=method)
    emit(db, UserLinkedToIdentity(tenant_id=user.tenant_id, user_id=user.id, idp_id=idp.id, method=method))


async def _jit(db: AsyncSession, tenant: Tenant, idp: IdentityProvider, claims: Claims) -> User:
    locked = await access.lock_tenant(db, tenant.id)
    if locked is None or await access.active_user_count(db) >= locked.seat_limit:
        raise Forbidden("This workspace has no free seats", code="seat_limit_reached")
    assert claims.email is not None
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email=claims.email,
        name=(claims.name or claims.email.split("@", 1)[0])[:200],
        # Unusable random password: SSO users sign in through their IdP.
        password_hash=hash_password(new_token()),
    )
    db.add(user)
    await db.flush()
    role_ids: set[uuid.UUID] = set()
    if idp.jit_default_role_id is not None:
        role_ids.add(idp.jit_default_role_id)
    if claims.groups:
        mapped = (
            await db.execute(
                select(IdentityProviderGroupRole.role_id).where(
                    IdentityProviderGroupRole.idp_id == idp.id,
                    IdentityProviderGroupRole.group_value.in_(claims.groups),
                )
            )
        ).scalars()
        role_ids.update(mapped)
    if role_ids:
        existing = set((await db.execute(select(Role.id).where(Role.id.in_(role_ids)))).scalars())
        await access.set_user_roles(db, tenant.id, user.id, existing)
    await _link(db, idp, user, claims, method="jit")
    audit.record("sso.user_jit_provisioned", "user", user.id, session=db, idp_id=idp.id, roles=len(role_ids))
    emit(db, UserJitProvisioned(tenant_id=tenant.id, user_id=user.id, idp_id=idp.id))
    return user


def error_code(exc: Exception) -> str:
    return exc.code if isinstance(exc, DomainError) else "sso_failed"
