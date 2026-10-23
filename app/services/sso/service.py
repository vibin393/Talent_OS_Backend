"""SSO administration (Epic 14, FR-77, AD-30): identity providers, group -> Role mapping, enforcement and the
break-glass Tenant Admins.

* The OIDC client secret is sealed with Fernet purpose ``sso`` and never returned (``has_secret`` only).
* Enforcement (``enforce_for`` all | roles) requires at least one active break-glass Tenant Admin with MFA
  enrolled (409 ``break_glass_required``); ``assert_break_glass_preserved`` re-checks it when a break-glass
  admin is demoted, deactivated or loses MFA.
* An IdP with linked users is disabled instead of deleted.
* Config changes are audited (row audit + explicit ``sso.*`` events).
* Epic 50 (AD-48): every query here serves internal IdPs only (``audience = 'internal'``); client IdPs are
  administered by ``client_admin`` and are invisible (404) to these endpoints, to enforcement and to the
  password gate.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.permissions import TENANT_ADMIN
from app.core.security import encrypt_str
from app.core.settings import get_settings
from app.events.sso import IdentityProviderChanged
from app.models.identity import Role, Tenant, User, UserRole
from app.models.sso import IdentityProvider, IdentityProviderGroupRole, UserIdentity
from app.schemas.sso import (
    BreakGlassAdminOut,
    GroupRoleIn,
    GroupRoleOut,
    IdentityProviderIn,
    IdentityProviderOut,
    IdentityProviderUpdate,
    TestLoginOut,
)


def _base() -> str:
    return get_settings().frontend_url.rstrip("/")


def redirect_uri() -> str:
    return f"{_base()}/api/v1/auth/sso/oidc/callback"


def sp_entity_id(idp_id: uuid.UUID) -> str:
    return f"{_base()}/api/v1/sso/saml/{idp_id}/metadata"


def acs_url(idp_id: uuid.UUID) -> str:
    return f"{_base()}/api/v1/auth/sso/saml/{idp_id}/acs"


def _tenant(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "sso needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def _name_taken() -> ConflictError:
    return ConflictError(
        "Another identity provider already has this name", code="identity_provider_name_taken"
    )


INTERNAL = IdentityProvider.audience == "internal"


async def _get(db: AsyncSession, idp_id: uuid.UUID, *, lock: bool = False) -> IdentityProvider:
    stmt = select(IdentityProvider).where(IdentityProvider.id == idp_id, INTERNAL)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Identity provider not found")
    return row


async def _out(db: AsyncSession, row: IdentityProvider) -> IdentityProviderOut:
    linked = int((await db.execute(select(func.count()).where(UserIdentity.idp_id == row.id))).scalar_one())
    groups = (
        await db.execute(
            select(IdentityProviderGroupRole)
            .where(IdentityProviderGroupRole.idp_id == row.id)
            .order_by(IdentityProviderGroupRole.group_value)
        )
    ).scalars()
    return IdentityProviderOut(
        id=row.id,
        name=row.name,
        kind=row.kind,  # type: ignore[arg-type]
        status=row.status,  # type: ignore[arg-type]
        email_domains=list(row.email_domains or []),
        config=dict(row.config or {}),
        has_secret=row.secret is not None,
        enforce_for=row.enforce_for,  # type: ignore[arg-type]
        enforced_role_ids=list(row.enforced_role_ids or []),
        jit_enabled=row.jit_enabled,
        jit_default_role_id=row.jit_default_role_id,
        group_claim=row.group_claim,
        mfa_trusted=row.mfa_trusted,
        linked_users=linked,
        group_roles=[GroupRoleOut(group_value=g.group_value, role_id=g.role_id) for g in groups],
        redirect_uri=redirect_uri() if row.kind == "oidc" else None,
        sp_entity_id=sp_entity_id(row.id) if row.kind == "saml" else None,
        acs_url=acs_url(row.id) if row.kind == "saml" else None,
        metadata_url=sp_entity_id(row.id) if row.kind == "saml" else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _config_for(kind: str, oidc: Any, saml: Any) -> dict[str, Any]:
    if kind == "oidc":
        if oidc is None:
            raise ValidationFailed(
                "OIDC settings are required", code="sso_config_required", details={"field": "oidc"}
            )
        return oidc.model_dump(exclude_none=True)
    if saml is None:
        raise ValidationFailed(
            "SAML settings are required", code="sso_config_required", details={"field": "saml"}
        )
    return saml.model_dump(exclude_none=True)


async def _check_roles(db: AsyncSession, role_ids: list[uuid.UUID], field: str) -> None:
    if not role_ids:
        return
    found = set((await db.execute(select(Role.id).where(Role.id.in_(role_ids)))).scalars())
    missing = [str(r) for r in role_ids if r not in found]
    if missing:
        raise ValidationFailed(
            "Unknown role", code="invalid_reference", details={"field": field, "ids": missing}
        )


async def _domains_free(
    db: AsyncSession, domains: list[str], exclude: uuid.UUID | None, *, audience: str = "internal"
) -> None:
    """Refuse a domain already served by another active IdP of the same audience (discovery must be
    unambiguous; internal and client discovery are separate endpoints)."""
    if not domains:
        return
    stmt = select(IdentityProvider).where(
        IdentityProvider.status == "active", IdentityProvider.audience == audience
    )
    if exclude is not None:
        stmt = stmt.where(IdentityProvider.id != exclude)
    for other in (await db.execute(stmt)).scalars():
        clash = sorted(set(domains) & set(other.email_domains or []))
        if clash:
            raise ConflictError(
                "Another active identity provider already serves this email domain",
                code="sso_domain_taken",
                details={"domains": clash},
            )


# ================================================================ break-glass admins
async def _break_glass_ok(db: AsyncSession, *, losing_user_id: uuid.UUID | None = None) -> bool:
    """At least one active Tenant Admin (other than ``losing_user_id``) with ``break_glass`` and MFA."""
    stmt = (
        select(func.count(func.distinct(User.id)))
        .select_from(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            Role.key == TENANT_ADMIN,
            User.is_active.is_(True),
            User.break_glass.is_(True),
            User.mfa_enabled.is_(True),
        )
    )
    if losing_user_id is not None:
        stmt = stmt.where(User.id != losing_user_id)
    return int((await db.execute(stmt)).scalar_one()) > 0


def _break_glass_required() -> ConflictError:
    return ConflictError(
        "SSO enforcement needs at least one active break-glass Tenant Admin with MFA enrolled",
        code="break_glass_required",
    )


async def enforcement_active(db: AsyncSession) -> bool:
    return (
        await db.execute(
            select(IdentityProvider.id)
            .where(IdentityProvider.status == "active", IdentityProvider.enforce_for != "none", INTERNAL)
            .limit(1)
        )
    ).first() is not None


async def assert_break_glass_preserved(db: AsyncSession, losing_user_id: uuid.UUID) -> None:
    """Refuse (409) before a break-glass admin is demoted / deactivated / loses MFA (identity call sites)."""
    if not await enforcement_active(db):
        return
    user = await db.get(User, losing_user_id)
    if user is None or not user.break_glass:
        return
    if not await _break_glass_ok(db, losing_user_id=losing_user_id):
        raise _break_glass_required()


async def list_break_glass(ctx: RequestContext, db: AsyncSession) -> list[BreakGlassAdminOut]:  # ruff: ignore[unused-function-argument]
    rows = (
        await db.execute(
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.key == TENANT_ADMIN)
            .order_by(User.name)
        )
    ).scalars()
    return [
        BreakGlassAdminOut(
            user_id=u.id,
            name=u.name,
            email=u.email,
            is_active=u.is_active,
            mfa_enabled=u.mfa_enabled,
            break_glass=u.break_glass,
        )
        for u in rows
    ]


async def set_break_glass(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    user_id: uuid.UUID,
    break_glass: bool,
) -> BreakGlassAdminOut:
    user = await db.get(User, user_id, with_for_update=True)
    if user is None:
        raise NotFound("User not found")
    is_admin = (
        await db.execute(
            select(UserRole.user_id)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == user_id, Role.key == TENANT_ADMIN)
        )
    ).first() is not None
    if break_glass and not is_admin:
        raise ValidationFailed("Only Tenant Admins can be break-glass admins", code="break_glass_not_admin")
    if not break_glass and user.break_glass:
        await assert_break_glass_preserved(db, user.id)
    if user.break_glass != break_glass:
        user.break_glass = break_glass
        await db.flush()
        audit.record("sso.break_glass_changed", "user", user.id, session=db, break_glass=break_glass)
    return BreakGlassAdminOut(
        user_id=user.id,
        name=user.name,
        email=user.email,
        is_active=user.is_active,
        mfa_enabled=user.mfa_enabled,
        break_glass=user.break_glass,
    )


# ================================================================ identity providers
async def list_providers(ctx: RequestContext, db: AsyncSession) -> list[IdentityProviderOut]:  # ruff: ignore[unused-function-argument]
    rows = (
        (await db.execute(select(IdentityProvider).where(INTERNAL).order_by(IdentityProvider.name)))
        .scalars()
        .all()
    )
    return [await _out(db, r) for r in rows]


async def get_provider(ctx: RequestContext, db: AsyncSession, idp_id: uuid.UUID) -> IdentityProviderOut:  # ruff: ignore[unused-function-argument]
    return await _out(db, await _get(db, idp_id))


async def create_provider(
    ctx: RequestContext, db: AsyncSession, data: IdentityProviderIn
) -> IdentityProviderOut:
    config = _config_for(data.kind, data.oidc, data.saml)
    await _check_roles(db, data.enforced_role_ids, "enforced_role_ids")
    if data.jit_default_role_id is not None:
        await _check_roles(db, [data.jit_default_role_id], "jit_default_role_id")
    principal = ctx.principal
    row = IdentityProvider(
        id=uuid.uuid4(),
        tenant_id=_tenant(db),
        name=data.name,
        kind=data.kind,
        audience="internal",
        status="draft",
        email_domains=sorted(set(data.email_domains)),
        config=config,
        secret=encrypt_str("sso", data.client_secret) if data.client_secret else None,
        enforce_for=data.enforce_for,
        enforced_role_ids=data.enforced_role_ids,
        jit_enabled=data.jit_enabled,
        jit_default_role_id=data.jit_default_role_id,
        group_claim=data.group_claim,
        mfa_trusted=data.mfa_trusted,
        created_by=getattr(principal, "id", None) if getattr(principal, "type", None) == "user" else None,
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        raise _name_taken() from exc
    audit.record("sso.identity_provider_created", "identity_provider", row.id, session=db, kind=row.kind)
    emit(db, IdentityProviderChanged(tenant_id=row.tenant_id, idp_id=row.id, change="created"))
    await db.refresh(row)
    return await _out(db, row)


async def update_provider(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    idp_id: uuid.UUID,
    data: IdentityProviderUpdate,
) -> IdentityProviderOut:
    row = await _get(db, idp_id, lock=True)
    sent = data.model_fields_set
    if "name" in sent and data.name:
        row.name = data.name
    if "email_domains" in sent and data.email_domains is not None:
        row.email_domains = sorted(set(data.email_domains))
    if row.kind == "oidc" and data.oidc is not None:
        row.config = data.oidc.model_dump(exclude_none=True)
    if row.kind == "saml" and data.saml is not None:
        row.config = data.saml.model_dump(exclude_none=True)
    if "client_secret" in sent and data.client_secret:
        row.secret = encrypt_str("sso", data.client_secret)
    if "enforced_role_ids" in sent and data.enforced_role_ids is not None:
        await _check_roles(db, data.enforced_role_ids, "enforced_role_ids")
        row.enforced_role_ids = data.enforced_role_ids
    if "enforce_for" in sent and data.enforce_for is not None:
        row.enforce_for = data.enforce_for
    if "jit_enabled" in sent and data.jit_enabled is not None:
        row.jit_enabled = data.jit_enabled
    if "jit_default_role_id" in sent:
        if data.jit_default_role_id is not None:
            await _check_roles(db, [data.jit_default_role_id], "jit_default_role_id")
        row.jit_default_role_id = data.jit_default_role_id
    if "group_claim" in sent:
        row.group_claim = data.group_claim
    if "mfa_trusted" in sent and data.mfa_trusted is not None:
        row.mfa_trusted = data.mfa_trusted
    change = "updated"
    if "status" in sent and data.status is not None and data.status != row.status:
        if data.status == "active":
            _assert_activatable(row)
        row.status = data.status
        change = {"active": "activated", "disabled": "disabled"}.get(data.status, "updated")
    if row.status == "active":
        await _domains_free(db, list(row.email_domains or []), row.id)
        if row.enforce_for != "none" and not await _break_glass_ok(db):
            raise _break_glass_required()
        if row.enforce_for == "roles" and not row.enforced_role_ids:
            raise ValidationFailed(
                "Choose the roles that must use SSO",
                code="sso_enforced_roles_required",
                details={"field": "enforced_role_ids"},
            )
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise _name_taken() from exc
    audit.record(
        "sso.identity_provider_changed",
        "identity_provider",
        row.id,
        session=db,
        change=change,
        fields=sorted(sent - {"client_secret"}) + (["client_secret"] if "client_secret" in sent else []),
    )
    emit(db, IdentityProviderChanged(tenant_id=row.tenant_id, idp_id=row.id, change=change))
    await db.refresh(row)
    return await _out(db, row)


def _assert_activatable(row: IdentityProvider) -> None:
    config = row.config or {}
    if row.kind == "oidc":
        missing = [k for k in ("issuer", "client_id") if not config.get(k)]
    else:
        missing = [k for k in ("idp_entity_id", "sso_url", "x509_certs") if not config.get(k)]
    if not row.email_domains:
        missing.append("email_domains")
    if missing:
        raise ValidationFailed(
            "Complete the identity provider before activating it",
            code="sso_config_incomplete",
            details={"missing": missing},
        )


async def delete_provider(ctx: RequestContext, db: AsyncSession, idp_id: uuid.UUID) -> None:  # ruff: ignore[unused-function-argument]
    """Delete when no User is linked; otherwise disable (links keep history)."""
    row = await _get(db, idp_id, lock=True)
    linked = (await db.execute(select(UserIdentity.id).where(UserIdentity.idp_id == row.id).limit(1))).first()
    if linked is not None:
        if row.status != "disabled":
            row.status = "disabled"
            await db.flush()
            audit.record(
                "sso.identity_provider_changed", "identity_provider", row.id, session=db, change="disabled"
            )
            emit(db, IdentityProviderChanged(tenant_id=row.tenant_id, idp_id=row.id, change="disabled"))
        return
    tenant_id = row.tenant_id
    await db.delete(row)
    await db.flush()
    audit.record("sso.identity_provider_deleted", "identity_provider", idp_id, session=db)
    emit(db, IdentityProviderChanged(tenant_id=tenant_id, idp_id=idp_id, change="deleted"))


async def set_group_roles(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    idp_id: uuid.UUID,
    mappings: list[GroupRoleIn],
) -> IdentityProviderOut:
    row = await _get(db, idp_id, lock=True)
    await _check_roles(db, [m.role_id for m in mappings], "role_id")
    existing = (
        await db.execute(select(IdentityProviderGroupRole).where(IdentityProviderGroupRole.idp_id == row.id))
    ).scalars()
    for old in existing:
        await db.delete(old)
    await db.flush()
    for m in {(m.group_value, m.role_id) for m in mappings}:
        db.add(
            IdentityProviderGroupRole(
                id=uuid.uuid4(), tenant_id=row.tenant_id, idp_id=row.id, group_value=m[0], role_id=m[1]
            )
        )
    await db.flush()
    audit.record("sso.group_roles_changed", "identity_provider", row.id, session=db, count=len(mappings))
    return await _out(db, row)


async def test_login_url(ctx: RequestContext, db: AsyncSession, idp_id: uuid.UUID) -> TestLoginOut:
    """A sign-in URL that runs the whole flow, shows the mapped claims and creates nothing."""
    row = await _get(db, idp_id)
    tenant = await db.get(Tenant, row.tenant_id)
    assert tenant is not None
    del ctx
    return TestLoginOut(url=f"/api/v1/auth/sso/{row.id}/start?tenant={tenant.slug}&test=1")


async def sp_metadata(ctx: RequestContext, db: AsyncSession, idp_id: uuid.UUID) -> str:  # ruff: ignore[unused-function-argument]
    from app.services.sso import saml

    row = await _get(db, idp_id)
    if row.kind != "saml":
        raise NotFound("Identity provider not found")
    config = dict(row.config or {})
    config.setdefault("idp_entity_id", "unset")
    config.setdefault("sso_url", "https://unset.invalid/sso")
    return saml.metadata_xml(config, sp_entity_id=sp_entity_id(row.id), acs_url=acs_url(row.id))


# ================================================================ enforcement (password path)
async def password_allowed(db: AsyncSession, user: User) -> bool:
    """Return False when an active IdP enforces SSO for everyone or for one of the user's Roles.

    Break-glass admins with MFA enrolled are always allowed (AD-30).
    """
    if user.break_glass and user.mfa_enabled:
        return True
    rows = (
        await db.execute(
            select(IdentityProvider.enforce_for, IdentityProvider.enforced_role_ids).where(
                IdentityProvider.status == "active", IdentityProvider.enforce_for != "none", INTERNAL
            )
        )
    ).all()
    if not rows:
        return True
    role_ids = set((await db.execute(select(UserRole.role_id).where(UserRole.user_id == user.id))).scalars())
    return not any(enforce_for == "all" or role_ids & set(enforced or []) for enforce_for, enforced in rows)
