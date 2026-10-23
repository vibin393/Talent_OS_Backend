"""Client Portal SSO sign-in (Epic 50, FR-251, AD-48, NFR-41): resolution of a client IdP assertion.

A ``client`` IdP touches **only** ``client_users`` with ``client_id = idp.client_id``; it never touches
``users``,
``vendor_users`` or candidates (they are read only to name the refusal reason in the audit log, never linked):

1. ``client_user_identities`` by ``(idp_id, subject)`` -> the Client User (must belong to ``idp.client_id``,
   else refused ``other_client`` -- defence in depth);
2. else a verified email (OIDC ``email_verified`` / signed SAML assertion) whose domain is in the IdP's
   ``email_domains`` (AD-48 ``allowed_domains``) -> the Client User of ``idp.client_id`` with that email
   (``active`` | ``invited``) -> link (audited ``client_sso.identity_linked``);
3. else JIT when the IdP enables it (``external_auth.access.jit_client_user``) -- never when the email already
   belongs to a Client User of another Client, an internal User or a Vendor User;
4. else refused 401 ``sso_no_client_user`` (generic to the caller; the reason goes to the audit log).

The session is opened only by ``external_auth.signin.open_client_session_via_sso`` (``tos_client``, org GUCs
of the Client, AD-26). Test logins report the resolution and create nothing.
"""

import dataclasses
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, rate_limit
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import DomainError, Forbidden, Unauthorized
from app.core.events import emit
from app.events.sso import ClientUserJitProvisioned, ClientUserLinkedToIdentity
from app.models.external_auth import ClientUser, VendorUser
from app.models.identity import Tenant, User
from app.models.sso import ClientUserIdentity, IdentityProvider
from app.schemas.client_sso import ClientSsoDiscoverIn, ClientSsoDiscoverOut, ClientSsoTestResultOut
from app.services import auth_service
from app.services.external_auth import access as external_access
from app.services.external_auth import signin as external_signin

if TYPE_CHECKING:
    from app.models.sso import SsoLoginState
    from app.services.sso.login import Claims, LoginResult

USABLE = ("active", "invited")


class Refused(Unauthorized):
    """A refused client sign-in: generic ``code`` for the caller, specific ``reason`` for the audit log."""

    def __init__(self, code: str, reason: str, message: str) -> None:
        super().__init__(message, code=code)
        self.reason = reason


def _no_client_user(reason: str) -> Refused:
    return Refused("sso_no_client_user", reason, "No client portal account exists for this sign-in")


def _inactive() -> Forbidden:
    return Forbidden("Your client portal access is suspended or revoked", code="client_user_inactive")


@dataclasses.dataclass(frozen=True, slots=True)
class Resolution:
    kind: str  # linked | email_match | jit
    client_user: ClientUser | None  # None only for a dry-run ``jit``


def _now() -> datetime:
    return datetime.now(UTC)


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


async def _client_idp(db: AsyncSession, idp_id: uuid.UUID) -> IdentityProvider:
    idp = (
        await db.execute(
            select(IdentityProvider).where(
                IdentityProvider.id == idp_id,
                IdentityProvider.audience == "client",
                IdentityProvider.client_id.is_not(None),
            )
        )
    ).scalar_one_or_none()
    if idp is None or idp.status == "disabled":
        from app.services.sso import oidc

        raise oidc.invalid("identity_provider")
    return idp


async def _collision(db: AsyncSession, client_id: uuid.UUID, email: str) -> str | None:
    """Why an unknown email must not be provisioned here: it belongs to another Client's user or to another
    principal type. Read-only (the refusal reason); nothing of these principals is ever linked or signed
    in."""
    other = (
        await db.execute(
            select(ClientUser.id).where(ClientUser.email == email, ClientUser.client_id != client_id).limit(1)
        )
    ).first()
    if other is not None:
        return "other_client"
    for model in (User, VendorUser):
        if (await db.execute(select(model.id).where(model.email == email).limit(1))).first() is not None:
            return "other_principal"
    return None


async def resolve(
    db: AsyncSession, idp: IdentityProvider, claims: "Claims", *, dry_run: bool = False
) -> Resolution:
    """Resolve (and, unless ``dry_run``, link / provision) the Client User of ``idp.client_id``."""
    client_id = idp.client_id
    assert client_id is not None
    link = (
        await db.execute(
            select(ClientUserIdentity).where(
                ClientUserIdentity.idp_id == idp.id, ClientUserIdentity.subject == claims.subject[:255]
            )
        )
    ).scalar_one_or_none()
    if link is not None:
        cu = (
            await db.execute(select(ClientUser).where(ClientUser.id == link.client_user_id))
        ).scalar_one_or_none()
        if cu is None or cu.client_id != client_id:
            raise _no_client_user("other_client")
        if cu.status not in USABLE:
            raise _inactive()
        return Resolution("linked", cu)
    if not claims.email or not claims.email_verified:
        raise Refused(
            "sso_email_unverified",
            "email_unverified",
            "Your identity provider did not confirm your email address",
        )
    email = claims.email.lower()
    if _domain(email) not in {d.lower() for d in idp.email_domains or []}:
        raise Refused(
            "sso_domain_not_allowed",
            "domain_not_allowed",
            "This email domain is not allowed for this sign-in",
        )
    stmt = (
        select(ClientUser)
        .where(ClientUser.client_id == client_id, ClientUser.email == email)
        .order_by(case((ClientUser.status == "active", 0), (ClientUser.status == "invited", 1), else_=2))
    )
    if not dry_run:
        stmt = stmt.with_for_update()
    cu = next(iter((await db.execute(stmt)).scalars()), None)
    if cu is not None:
        if cu.status not in USABLE:
            raise _inactive()
        taken = (
            await db.execute(
                select(ClientUserIdentity.id).where(
                    ClientUserIdentity.idp_id == idp.id, ClientUserIdentity.client_user_id == cu.id
                )
            )
        ).first()
        if taken is not None:
            # Already linked to another subject of this IdP: never re-link silently.
            raise Refused(
                "sso_identity_conflict", "identity_conflict", "This account is linked to another identity"
            )
        if not dry_run:
            await _link(db, idp, cu, claims, method="verified_email")
        return Resolution("email_match", cu)
    collision = await _collision(db, client_id, email)
    if collision is not None:
        raise _no_client_user(collision)
    if not idp.jit_enabled:
        raise _no_client_user("no_client_user")
    if dry_run:
        return Resolution("jit", None)
    cu = await external_access.jit_client_user(
        db,
        client_id,
        email=email,
        name=claims.name or email.split("@", 1)[0],
        permissions=list(idp.jit_default_permissions or []),
    )
    await _link(db, idp, cu, claims, method="jit")
    audit.record(
        "client_sso.user_jit_provisioned",
        "client_user",
        cu.id,
        session=db,
        idp_id=str(idp.id),
        client_id=str(client_id),
        permissions=sorted(cu.portal_permissions or []),
    )
    emit(
        db,
        ClientUserJitProvisioned(
            tenant_id=cu.tenant_id, client_user_id=cu.id, client_id=client_id, idp_id=idp.id
        ),
    )
    return Resolution("jit", cu)


async def _link(
    db: AsyncSession, idp: IdentityProvider, cu: ClientUser, claims: "Claims", *, method: str
) -> None:
    db.add(
        ClientUserIdentity(
            id=uuid.uuid4(),
            tenant_id=cu.tenant_id,
            client_user_id=cu.id,
            idp_id=idp.id,
            subject=claims.subject[:255],
            email_at_link=claims.email,
        )
    )
    await db.flush()
    audit.record(
        "client_sso.identity_linked", "client_user", cu.id, session=db, idp_id=str(idp.id), method=method
    )
    emit(
        db,
        ClientUserLinkedToIdentity(
            tenant_id=cu.tenant_id, client_user_id=cu.id, client_id=cu.client_id, idp_id=idp.id, method=method
        ),
    )


async def finish(
    ctx: RequestContext, tenant: Tenant, login_state: "SsoLoginState", claims: "Claims"
) -> "LoginResult":
    """Complete a client IdP login (or report a test login) after the assertion was validated."""
    from app.services.sso.login import LoginResult, safe_client_next, tag_audience

    next_path = safe_client_next(login_state.relay_to)
    if login_state.is_test:
        return LoginResult(
            next_path=next_path,
            test=await test_report(tenant.id, login_state.idp_id, claims),
            audience="client",
        )
    try:
        # Any refusal rolls the whole attempt back (no link, no JIT rows, no session).
        async with tenant_session(tenant.id) as db:
            audit.set_actor(db, "client_user", None)
            idp = await _client_idp(db, login_state.idp_id)
            resolved = await resolve(db, idp, claims)
            cu = resolved.client_user
            assert cu is not None
            audit.set_actor(db, "client_user", cu.id)
            await db.execute(
                update(ClientUserIdentity)
                .where(ClientUserIdentity.idp_id == idp.id, ClientUserIdentity.client_user_id == cu.id)
                .values(last_login_at=_now())
            )
            signed_in = await external_signin.open_client_session_via_sso(
                ctx, db, cu, method=f"sso_{idp.kind}", mfa_satisfied=idp.mfa_trusted
            )
    except DomainError as exc:
        await _audit_refusal(tenant.id, login_state.idp_id, exc)
        raise tag_audience(exc, "client") from None
    return LoginResult(next_path=next_path, client=signed_in, audience="client")


async def _audit_refusal(tenant_id: uuid.UUID, idp_id: uuid.UUID, error: DomainError) -> None:
    """Separate transaction: the failed attempt's writes (links, JIT rows) were rolled back with it."""
    reason = getattr(error, "reason", None) or (
        "inactive" if error.code == "client_user_inactive" else error.code
    )
    async with tenant_session(tenant_id) as db:
        audit.set_actor(db, "client_user", None)
        audit.record(
            "client_sso.login_refused",
            "identity_provider",
            idp_id,
            session=db,
            reason=reason,
            code=error.code,
        )


async def test_report(tenant_id: uuid.UUID, idp_id: uuid.UUID, claims: "Claims") -> ClientSsoTestResultOut:
    """Test sign-in: the mapped claims and the client resolution; nothing is linked or created."""
    async with tenant_session(tenant_id) as db:
        idp = await _client_idp(db, idp_id)
        reason: str | None = None
        cu_id: uuid.UUID | None = None
        try:
            resolved = await resolve(db, idp, claims, dry_run=True)
            kind = resolved.kind
            cu_id = resolved.client_user.id if resolved.client_user is not None else None
        except DomainError as exc:
            kind = "refused"
            reason = getattr(exc, "reason", None) or (
                "inactive" if exc.code == "client_user_inactive" else exc.code
            )
        audit.record(
            "client_sso.test_login", "identity_provider", idp.id, session=db, resolution=kind, reason=reason
        )
    return ClientSsoTestResultOut(
        subject=claims.subject,
        email=claims.email,
        email_verified=claims.email_verified,
        name=claims.name,
        groups=claims.groups,
        resolution=kind,
        reason=reason,
        client_user_id=cu_id,
    )


# ================================================================ Client Portal discovery
DISCOVER_IP_LIMIT = 30
DISCOVER_WINDOW_SECONDS = 600


async def discover(ctx: RequestContext, data: ClientSsoDiscoverIn) -> ClientSsoDiscoverOut:
    """Email-domain match against active client IdPs only. Unknown tenants, unknown domains and domains
    without client SSO answer ``{sso: false}`` with the same shape and padded timing (no enumeration)."""
    started = time.monotonic()
    try:
        await rate_limit.enforce(
            "client_sso_discover_ip",
            rate_limit.subject_hash(ctx.ip or "unknown"),
            DISCOVER_IP_LIMIT,
            DISCOVER_WINDOW_SECONDS,
        )
        tenant = await auth_service.tenant_by_slug(data.tenant_slug)
        if tenant is None:
            return ClientSsoDiscoverOut(sso=False)
        domain = _domain(str(data.email))
        async with tenant_session(tenant.id) as db:
            rows = (
                await db.execute(
                    select(IdentityProvider)
                    .where(
                        IdentityProvider.audience == "client",
                        IdentityProvider.status == "active",
                        IdentityProvider.client_id.is_not(None),
                    )
                    .order_by(IdentityProvider.created_at)
                )
            ).scalars()
            match = next((r for r in rows if domain in {d.lower() for d in r.email_domains or []}), None)
            if match is None or match.client_id is None:
                return ClientSsoDiscoverOut(sso=False)
            from app.services.sso.client_admin import _settings_row

            settings = await _settings_row(db, match.client_id)
            return ClientSsoDiscoverOut(
                sso=True, idp_id=match.id, required=bool(settings and settings.sso_required)
            )
    finally:
        await external_signin._pad(started)
