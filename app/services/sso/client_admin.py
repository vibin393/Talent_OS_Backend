"""Client Portal SSO administration (Epic 50, FR-250 / FR-252, AD-48): per-Client identity providers, the
Client's SSO enforcement, approval-link opt-out and break-glass Client Users.

* A client IdP serves exactly one Client (``audience='client'``, ``client_id``; DB CHECK). Its email domains
  (AD-48 ``allowed_domains`` == ``email_domains``) are required and unique among active client IdPs (409
  ``sso_domain_taken``); ``jit_default_permissions`` must be Client Portal permissions.
* Validation on save / activate: OIDC issuer discovery reachable + client id; SAML certificate parses and is
  not expired (warning ``certificate_expires_soon`` under 30 days), SSO URL https, signed assertions forced
  (AD-30).
* Enforcement (``client_sso_settings``): requiring SSO needs an active client IdP (409
  ``client_idp_required``)
  and warns without a break-glass user (``no_break_glass``). Exempt users must belong to the Client and get
  ``mfa_enforced``.
* Every change is audited (``client_sso.*``) and emits ``ClientIdentityProviderChanged`` (notification
  ``client_sso.config_changed`` to the Client's Account Manager and the ``client_sso:manage`` holders).

Every query filters ``audience = 'client'`` and the path's ``client_id``.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, Forbidden, NotFound, ValidationFailed
from app.core.events import emit
from app.core.security import encrypt_str
from app.events.sso import ClientIdentityProviderChanged
from app.models.external_auth import CLIENT_PORTAL_PERMISSIONS, ClientUser
from app.models.identity import Tenant
from app.models.sso import ClientSsoExemption, ClientSsoSettings, ClientUserIdentity, IdentityProvider
from app.schemas.client_sso import (
    ClientIdentityProviderIn,
    ClientIdentityProviderOut,
    ClientIdentityProviderUpdate,
    ClientSsoExemptionIn,
    ClientSsoExemptionOut,
    ClientSsoSettingsIn,
    ClientSsoSettingsOut,
    ClientTestLoginOut,
    MetadataImportIn,
)
from app.services.external_auth import access as external_access
from app.services.external_auth import signin as external_signin
from app.services.sso import oidc, saml
from app.services.sso.service import _domains_free, _name_taken, _tenant, acs_url, redirect_uri, sp_entity_id

CERT_WARNING = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(UTC)


def _user_id(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    return getattr(principal, "id", None) if getattr(principal, "type", None) == "user" else None


async def _assert_client(db: AsyncSession, client_id: uuid.UUID) -> None:
    from app.models.clients import Client

    if (await db.execute(select(Client.id).where(Client.id == client_id))).first() is None:
        raise NotFound("Client not found")


async def _get(
    db: AsyncSession, client_id: uuid.UUID, idp_id: uuid.UUID, *, lock: bool = False
) -> IdentityProvider:
    stmt = select(IdentityProvider).where(
        IdentityProvider.id == idp_id,
        IdentityProvider.audience == "client",
        IdentityProvider.client_id == client_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Identity provider not found")
    return row


def _changed(row: IdentityProvider, change: str) -> ClientIdentityProviderChanged:
    assert row.client_id is not None
    return ClientIdentityProviderChanged(
        tenant_id=row.tenant_id, idp_id=row.id, client_id=row.client_id, change=change
    )


# ================================================================ validation
def _domains(domains: list[str]) -> list[str]:
    clean = sorted({d.lower() for d in domains})
    if not clean:
        raise ValidationFailed(
            "A client identity provider needs at least one email domain",
            code="allowed_domains_required",
            details={"field": "email_domains"},
        )
    return clean


def _permissions(perms: list[str]) -> list[str]:
    unknown = sorted(set(perms) - set(CLIENT_PORTAL_PERMISSIONS))
    if unknown:
        raise ValidationFailed(
            "Choose Client Portal permissions only",
            code="invalid_portal_permission",
            details={"field": "jit_default_permissions", "permissions": unknown},
        )
    return sorted(set(perms))


def _config_for(kind: str, oidc_cfg: Any, saml_cfg: Any) -> dict[str, Any]:
    if kind == "oidc":
        return oidc_cfg.model_dump(exclude_none=True) if oidc_cfg is not None else {}
    return saml_cfg.model_dump(exclude_none=True) if saml_cfg is not None else {}


def _saml_warnings(config: dict[str, Any]) -> list[str]:
    """Raise on an unusable SAML config; return non-blocking warnings."""
    warnings: list[str] = []
    sso_url = str(config.get("sso_url") or "")
    if sso_url and not sso_url.lower().startswith("https://"):
        raise ValidationFailed(
            "The identity provider sign-in URL must use https",
            code="sso_https_required",
            details={"field": "sso_url"},
        )
    now = _now()
    for cert in config.get("x509_certs") or []:
        try:
            not_after = saml.certificate_not_after(str(cert))
        except ValueError as exc:
            raise ValidationFailed(
                "The signing certificate could not be read",
                code="sso_certificate_invalid",
                details={"field": "x509_certs"},
            ) from exc
        if not_after <= now:
            raise ValidationFailed(
                "The signing certificate has expired",
                code="sso_certificate_expired",
                details={"field": "x509_certs", "not_after": not_after.isoformat()},
            )
        if not_after - now < CERT_WARNING and "certificate_expires_soon" not in warnings:
            warnings.append("certificate_expires_soon")
    return warnings


def _warnings(row: IdentityProvider) -> list[str]:
    if row.kind != "saml":
        return []
    try:
        return _saml_warnings(dict(row.config or {}))
    except ValidationFailed as exc:
        return [exc.code]


async def _assert_activatable(row: IdentityProvider) -> None:
    config = dict(row.config or {})
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
    if row.kind == "oidc":
        try:
            await oidc.discovery(config)
        except Exception as exc:
            raise ValidationFailed(
                "The identity provider's discovery document could not be read",
                code="sso_discovery_failed",
                details={"field": "issuer"},
            ) from exc
    else:
        _saml_warnings(config)


# ================================================================ output
async def _out(db: AsyncSession, row: IdentityProvider) -> ClientIdentityProviderOut:
    linked, last = (
        await db.execute(
            select(func.count(ClientUserIdentity.id), func.max(ClientUserIdentity.last_login_at)).where(
                ClientUserIdentity.idp_id == row.id
            )
        )
    ).one()
    assert row.client_id is not None
    return ClientIdentityProviderOut(
        id=row.id,
        client_id=row.client_id,
        name=row.name,
        kind=row.kind,
        status=row.status,
        email_domains=list(row.email_domains or []),
        config=dict(row.config or {}),
        has_secret=row.secret is not None,
        jit_enabled=row.jit_enabled,
        jit_default_permissions=list(row.jit_default_permissions or []),
        mfa_trusted=row.mfa_trusted,
        linked_users=int(linked or 0),
        last_login_at=last,
        redirect_uri=redirect_uri() if row.kind == "oidc" else None,
        sp_entity_id=sp_entity_id(row.id) if row.kind == "saml" else None,
        acs_url=acs_url(row.id) if row.kind == "saml" else None,
        warnings=_warnings(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ================================================================ identity providers
async def list_providers(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
) -> list[ClientIdentityProviderOut]:
    await _assert_client(db, client_id)
    rows = (
        (
            await db.execute(
                select(IdentityProvider)
                .where(IdentityProvider.audience == "client", IdentityProvider.client_id == client_id)
                .order_by(IdentityProvider.name)
            )
        )
        .scalars()
        .all()
    )
    return [await _out(db, r) for r in rows]


async def get_provider(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    idp_id: uuid.UUID,
) -> ClientIdentityProviderOut:
    await _assert_client(db, client_id)
    return await _out(db, await _get(db, client_id, idp_id))


async def create_provider(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, data: ClientIdentityProviderIn
) -> ClientIdentityProviderOut:
    await _assert_client(db, client_id)
    domains = _domains(list(data.email_domains))
    perms = _permissions(list(data.jit_default_permissions))
    config = _config_for(data.kind, data.oidc, data.saml)
    if data.kind == "saml" and config:
        _saml_warnings(config)
    row = IdentityProvider(
        id=uuid.uuid4(),
        tenant_id=_tenant(db),
        name=data.name,
        kind=data.kind,
        status="draft",
        audience="client",
        client_id=client_id,
        email_domains=domains,
        config=config,
        secret=encrypt_str("sso", data.client_secret) if data.client_secret else None,
        jit_enabled=data.jit_enabled,
        jit_default_permissions=perms,
        mfa_trusted=data.mfa_trusted,
        created_by=_user_id(ctx),
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        raise _name_taken() from exc
    audit.record(
        "client_sso.idp_created",
        "identity_provider",
        row.id,
        session=db,
        kind=row.kind,
        client_id=str(client_id),
    )
    emit(db, _changed(row, "created"))
    await db.refresh(row)
    return await _out(db, row)


async def update_provider(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    idp_id: uuid.UUID,
    data: ClientIdentityProviderUpdate,
) -> ClientIdentityProviderOut:
    await _assert_client(db, client_id)
    row = await _get(db, client_id, idp_id, lock=True)
    sent = data.model_fields_set
    if "name" in sent and data.name:
        row.name = data.name
    if "email_domains" in sent and data.email_domains is not None:
        row.email_domains = _domains(list(data.email_domains))
    if row.kind == "oidc" and data.oidc is not None:
        row.config = data.oidc.model_dump(exclude_none=True)
    if row.kind == "saml" and data.saml is not None:
        config = data.saml.model_dump(exclude_none=True)
        _saml_warnings(config)
        row.config = config
    if "client_secret" in sent and data.client_secret:
        row.secret = encrypt_str("sso", data.client_secret)
    if "jit_enabled" in sent and data.jit_enabled is not None:
        row.jit_enabled = data.jit_enabled
    if "jit_default_permissions" in sent and data.jit_default_permissions is not None:
        row.jit_default_permissions = _permissions(list(data.jit_default_permissions))
    if "mfa_trusted" in sent and data.mfa_trusted is not None:
        row.mfa_trusted = data.mfa_trusted
    change = "updated"
    if "status" in sent and data.status is not None and data.status != row.status:
        if data.status == "active":
            await _assert_activatable(row)
        row.status = data.status
        change = {"active": "activated", "disabled": "disabled"}.get(data.status, "updated")
    if row.status == "active":
        await _domains_free(db, list(row.email_domains or []), row.id, audience="client")
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise _name_taken() from exc
    fields = sorted(sent - {"client_secret"}) + (["client_secret"] if "client_secret" in sent else [])
    audit.record(f"client_sso.idp_{change}", "identity_provider", row.id, session=db, fields=fields)
    emit(db, _changed(row, change))
    await db.refresh(row)
    return await _out(db, row)


async def delete_provider(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    idp_id: uuid.UUID,
) -> None:
    """Delete when no Client User is linked; otherwise disable (links keep history)."""
    await _assert_client(db, client_id)
    row = await _get(db, client_id, idp_id, lock=True)
    linked = (
        await db.execute(select(ClientUserIdentity.id).where(ClientUserIdentity.idp_id == row.id).limit(1))
    ).first()
    if linked is not None:
        if row.status != "disabled":
            row.status = "disabled"
            await db.flush()
            audit.record("client_sso.idp_disabled", "identity_provider", row.id, session=db)
            emit(db, _changed(row, "disabled"))
        return
    event = _changed(row, "deleted")
    await db.delete(row)
    await db.flush()
    audit.record("client_sso.idp_deleted", "identity_provider", idp_id, session=db, client_id=str(client_id))
    emit(db, event)


async def import_metadata(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, idp_id: uuid.UUID, data: MetadataImportIn
) -> ClientIdentityProviderOut:
    """SAML: entity id, SSO URL and signing certificates from IdP metadata (URL or XML)."""
    await _assert_client(db, client_id)
    row = await _get(db, client_id, idp_id, lock=True)
    if row.kind != "saml":
        raise ValidationFailed("Metadata import is for SAML identity providers", code="sso_not_saml")
    try:
        xml = (
            data.metadata_xml
            if data.metadata_xml is not None
            else await saml.fetch_metadata(str(data.metadata_url))
        )
        parsed = saml.parse_metadata(xml)
    except ValueError as exc:
        raise ValidationFailed(
            "The metadata could not be read", code="sso_metadata_invalid", details={"reason": str(exc)}
        ) from exc
    except Exception as exc:  # network errors
        raise ValidationFailed(
            "The metadata could not be downloaded",
            code="sso_metadata_invalid",
            details={"reason": "unreachable"},
        ) from exc
    config = {**dict(row.config or {}), **parsed}
    _saml_warnings(config)
    row.config = config
    await db.flush()
    audit.record("client_sso.idp_updated", "identity_provider", row.id, session=db, fields=["metadata"])
    emit(db, _changed(row, "updated"))
    del ctx
    await db.refresh(row)
    return await _out(db, row)


async def test_login_url(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    idp_id: uuid.UUID,
) -> ClientTestLoginOut:
    """Runs the whole flow and reports the claims and the client resolution; creates nothing."""
    await _assert_client(db, client_id)
    row = await _get(db, client_id, idp_id)
    tenant = await db.get(Tenant, row.tenant_id)
    assert tenant is not None
    return ClientTestLoginOut(
        url=f"/api/v1/client-portal/auth/sso/{row.id}/start?tenant={tenant.slug}&test=1"
    )


async def sp_metadata(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    idp_id: uuid.UUID,
) -> str:
    await _assert_client(db, client_id)
    row = await _get(db, client_id, idp_id)
    if row.kind != "saml":
        raise NotFound("Identity provider not found")
    config = dict(row.config or {})
    config.setdefault("idp_entity_id", "unset")
    config.setdefault("sso_url", "https://unset.invalid/sso")
    return saml.metadata_xml(config, sp_entity_id=sp_entity_id(row.id), acs_url=acs_url(row.id))


# ================================================================ enforcement
async def _settings_row(
    db: AsyncSession, client_id: uuid.UUID, *, lock: bool = False
) -> ClientSsoSettings | None:
    stmt = select(ClientSsoSettings).where(ClientSsoSettings.client_id == client_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def _has_active_idp(db: AsyncSession, client_id: uuid.UUID) -> bool:
    return (
        await db.execute(
            select(IdentityProvider.id)
            .where(
                IdentityProvider.audience == "client",
                IdentityProvider.client_id == client_id,
                IdentityProvider.status == "active",
            )
            .limit(1)
        )
    ).first() is not None


async def _exemption_count(db: AsyncSession, client_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count(ClientSsoExemption.id))
                .join(ClientUser, ClientUser.id == ClientSsoExemption.client_user_id)
                .where(ClientUser.client_id == client_id)
            )
        ).scalar_one()
    )


async def _settings_out(db: AsyncSession, client_id: uuid.UUID) -> ClientSsoSettingsOut:
    row = await _settings_row(db, client_id)
    required = bool(row and row.sso_required)
    exemptions = await _exemption_count(db, client_id)
    tenant = await db.get(Tenant, _tenant(db))
    return ClientSsoSettingsOut(
        client_id=client_id,
        sso_required=required,
        approval_links_allowed=row.approval_links_allowed if row else True,
        has_active_idp=await _has_active_idp(db, client_id),
        exemption_count=exemptions,
        warnings=["no_break_glass"] if required and exemptions == 0 else [],
        sign_in_url=external_signin.client_portal_sign_in_url(tenant.slug if tenant else ""),
        updated_at=row.updated_at if row else None,
    )


async def get_settings(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID) -> ClientSsoSettingsOut:  # ruff: ignore[unused-function-argument]
    await _assert_client(db, client_id)
    return await _settings_out(db, client_id)


async def put_settings(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, data: ClientSsoSettingsIn
) -> ClientSsoSettingsOut:
    await _assert_client(db, client_id)
    row = await _settings_row(db, client_id, lock=True)
    if row is None:
        row = ClientSsoSettings(id=uuid.uuid4(), tenant_id=_tenant(db), client_id=client_id)
        db.add(row)
        await db.flush()
        await db.refresh(row)
    before = {"sso_required": row.sso_required, "approval_links_allowed": row.approval_links_allowed}
    if data.sso_required is True and not row.sso_required and not await _has_active_idp(db, client_id):
        raise ConflictError(
            "Activate an identity provider for this client before requiring SSO", code="client_idp_required"
        )
    if data.sso_required is not None:
        row.sso_required = data.sso_required
    if data.approval_links_allowed is not None:
        row.approval_links_allowed = data.approval_links_allowed
    after = {"sso_required": row.sso_required, "approval_links_allowed": row.approval_links_allowed}
    if after != before:
        row.updated_by = _user_id(ctx)
        await db.flush()
        audit.record(
            "client_sso.enforcement_changed", "client", client_id, session=db, before=before, after=after
        )
        emit(
            db,
            ClientIdentityProviderChanged(
                tenant_id=row.tenant_id, idp_id=None, client_id=client_id, change="enforcement_changed"
            ),
        )
    return await _settings_out(db, client_id)


async def list_exemptions(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
) -> list[ClientSsoExemptionOut]:
    await _assert_client(db, client_id)
    rows = (
        await db.execute(
            select(ClientSsoExemption, ClientUser)
            .join(ClientUser, ClientUser.id == ClientSsoExemption.client_user_id)
            .where(ClientUser.client_id == client_id)
            .order_by(ClientUser.display_name)
        )
    ).all()
    return [_exemption_out(e, cu) for e, cu in rows]


def _exemption_out(e: ClientSsoExemption, cu: ClientUser) -> ClientSsoExemptionOut:
    return ClientSsoExemptionOut(
        client_user_id=cu.id,
        display_name=cu.display_name,
        email=str(cu.email),
        status=cu.status,
        reason=e.reason,
        mfa_enforced=cu.mfa_enforced,
        mfa_enabled=cu.mfa_enabled_at is not None,
        created_at=e.created_at,
    )


async def add_exemption(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, data: ClientSsoExemptionIn
) -> ClientSsoExemptionOut:
    """Break-glass: the Client User (of this Client) keeps magic link + code, with mandatory TOTP."""
    await _assert_client(db, client_id)
    cu = await external_access.force_client_mfa(db, client_id, data.client_user_id)
    if cu.status == "revoked":
        raise ConflictError("Portal access was revoked", code="client_user_revoked")
    row = ClientSsoExemption(
        id=uuid.uuid4(),
        tenant_id=cu.tenant_id,
        client_user_id=cu.id,
        reason=data.reason,
        created_by=_user_id(ctx),
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError("This user is already a break-glass user", code="exemption_exists") from exc
    audit.record("client_sso.exemption_added", "client_user", cu.id, session=db, client_id=str(client_id))
    emit(
        db,
        ClientIdentityProviderChanged(
            tenant_id=cu.tenant_id, idp_id=None, client_id=client_id, change="exemption_added"
        ),
    )
    await db.refresh(row)
    await db.refresh(cu)
    return _exemption_out(row, cu)


async def remove_exemption(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    client_user_id: uuid.UUID,
) -> None:
    await _assert_client(db, client_id)
    row = (
        await db.execute(
            select(ClientSsoExemption)
            .join(ClientUser, ClientUser.id == ClientSsoExemption.client_user_id)
            .where(ClientSsoExemption.client_user_id == client_user_id, ClientUser.client_id == client_id)
            .with_for_update(of=ClientSsoExemption)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Break-glass user not found", code="exemption_not_found")
    tenant_id = row.tenant_id
    await db.delete(row)
    await db.flush()
    audit.record(
        "client_sso.exemption_removed", "client_user", client_user_id, session=db, client_id=str(client_id)
    )
    emit(
        db,
        ClientIdentityProviderChanged(
            tenant_id=tenant_id, idp_id=None, client_id=client_id, change="exemption_removed"
        ),
    )


# ============================================================ gates for external_auth / approval links
async def client_login_required(db: AsyncSession, client_user: ClientUser) -> bool:
    """SSO is required for this Client User: the Client requires it, has an active client IdP, and the user is
    not a break-glass exemption (FR-252)."""
    row = await _settings_row(db, client_user.client_id)
    if row is None or not row.sso_required:
        return False
    if not await _has_active_idp(db, client_user.client_id):
        return False
    exempt = (
        await db.execute(
            select(ClientSsoExemption.id).where(ClientSsoExemption.client_user_id == client_user.id)
        )
    ).first()
    return exempt is None


async def approval_links_allowed(db: AsyncSession, client_id: uuid.UUID) -> bool:
    row = await _settings_row(db, client_id)
    return row is None or row.approval_links_allowed


async def assert_approval_link_allowed(db: AsyncSession, client_user_id: uuid.UUID) -> None:
    """``client_approval`` Secure Links: 403 ``sso_required`` (with the portal sign-in URL) when the
    addressee's Client turned "Allow approval links by email" off. Default (no settings row) keeps P2
    behaviour."""
    client_id = (
        await db.execute(select(ClientUser.client_id).where(ClientUser.id == client_user_id))
    ).scalar_one_or_none()
    if client_id is None or await approval_links_allowed(db, client_id):
        return
    tenant = await db.get(Tenant, _tenant(db))
    raise Forbidden(
        "Your organisation requires signing in to the client portal to approve",
        code="sso_required",
        details={"sign_in_url": external_signin.client_portal_sign_in_url(tenant.slug if tenant else "")},
    )
