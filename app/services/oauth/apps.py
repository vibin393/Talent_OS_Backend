# ruff: file-ignore[non-imperative-mood, line-too-long]
"""App runtime status (Epic 66 core) and App management (Epic 67, story 67.1; spec A3).

* ``create_app`` / ``update_app`` / ``rotate_secret`` keep the frozen 66 signatures. Private Apps
  (``Publisher(kind="tenant")``) need ``app:develop`` and a human; Epic 74 calls the same functions for partners
  (``Publisher(kind="partner")``; the caller authorises, this module checks only that the partner org is active).
* Client secrets (``tos_cs_``) are returned once and stored as ``sha256`` (``app_secrets``); a rotation keeps the
  previous secret valid for ``oauth_secret_overlap_hours``; at most two unrevoked secrets exist.
* ``update_app`` bumps ``manifest_version`` when scopes, data flows or resources change; installations then carry
  ``pending_manifest_version`` (re-consent through ``marketplace_service.upgrade``) and keep working on their
  previous Grant.
"""

import base64
import hashlib
import json
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.database import platform_session
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.oauth_scopes import scopes as scope_catalogue
from app.core.permissions import Principal, check
from app.core.settings import get_settings
from app.events.ecosystem import InstallationChanged
from app.models.oauth import App, AppInstallation, AppSecret, PartnerOrg
from app.schemas.oauth import AppCreate, AppCreated, AppOut, AppStatus, AppUpdate, Publisher, SecretCreated
from app.services import ecosystem_service
from app.services.oauth.principals import clear_cache
from app.services.oauth.tokens import SECRET_PREFIX, hash_token, revoke_for

APP_STATUSES = ("development", "published", "suspended", "retired")
GRANTS = ("authorization_code", "client_credentials", "refresh_token")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
MAX_URI = 2000


def _now() -> datetime:
    return datetime.now(UTC)


def app_out(row: App, *, reconsent: int = 0) -> AppOut:
    manifest = row.manifest or {}
    return AppOut(
        id=row.id,
        client_id=row.client_id,
        name=row.name,
        slug=str(row.slug),
        publisher_kind=row.publisher_kind,
        client_type=row.client_type,
        grant_types=list(row.grant_types),
        resources=list(row.resources),
        requested_scopes=list(row.requested_scopes),
        status=row.status,
        manifest_version=row.manifest_version,
        created_at=row.created_at,
        description=manifest.get("description"),
        redirect_uris=list(row.redirect_uris or []),
        scope_reasons=dict(row.scope_reasons or {}),
        homepage_url=row.homepage_url,
        privacy_url=row.privacy_url,
        support_email=str(row.support_email) if row.support_email else None,
        owner_tenant_id=row.owner_tenant_id,
        reconsent_installations=reconsent,
    )


# ================================================================ validation (pure)
class DataFlowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: Literal["out", "in"]
    entity: str = Field(min_length=1, max_length=60)
    fields: list[str] = Field(default_factory=list, max_length=100)
    purpose: str = Field(min_length=1, max_length=300)
    destination_country: str = Field(default="IN", min_length=2, max_length=2)
    personal_data: bool = True


class ManifestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_flows: list[DataFlowIn] = Field(default_factory=list, max_length=50)
    events_consumed: list[str] = Field(default_factory=list, max_length=100)


def _fail(field: str, message: str, code: str = "validation_failed") -> ValidationFailed:
    return ValidationFailed(message, code=code, details={"fields": {field: message}})


def validate_redirect_uri(uri: str, client_type: str) -> str:
    """Exact-match redirect rules (spec A3): https, or loopback http for public clients; no fragment / wildcard."""
    if len(uri) > MAX_URI or "*" in uri or "#" in uri or not uri.strip() or uri != uri.strip():
        raise _fail("redirect_uris", f"Invalid redirect URI: {uri[:100]}")
    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError as exc:
        raise _fail("redirect_uris", f"Invalid redirect URI: {uri[:100]}") from exc
    host = (parts.hostname or "").lower()
    if not host or parts.username or parts.password:
        raise _fail("redirect_uris", f"Invalid redirect URI: {uri[:100]}")
    if parts.scheme == "https":
        return uri
    if parts.scheme == "http" and host in LOOPBACK_HOSTS and client_type == "public" and port is not None:
        return uri
    raise _fail(
        "redirect_uris",
        "Use https, or http://127.0.0.1:<port> or http://localhost:<port> for desktop and command-line apps",
    )


def validate_https_url(field: str, value: str | None) -> str | None:
    if value is None or value == "":
        return None
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise _fail(field, "Enter an https URL") from exc
    if parts.scheme != "https" or not parts.hostname or len(value) > MAX_URI:
        raise _fail(field, "Enter an https URL")
    return value


def validate_registration(
    *,
    client_type: str,
    grant_types: list[str],
    redirect_uris: list[str],
    resources: list[str],
    requested_scopes: list[str],
    scope_reasons: dict[str, str],
) -> tuple[list[str], list[str], list[str], list[str], dict[str, str]]:
    s = get_settings()
    grants = sorted(set(grant_types))
    if not grants or not set(grants) <= set(GRANTS):
        raise _fail("grant_types", "Choose how the app signs in")
    if client_type == "public" and "client_credentials" in grants:
        raise _fail("grant_types", "Only server-side apps can use client credentials")
    if len(redirect_uris) > s.app_redirect_uris_max:
        raise _fail("redirect_uris", f"At most {s.app_redirect_uris_max} redirect URIs")
    uris = [validate_redirect_uri(u, client_type) for u in redirect_uris]
    if len(set(uris)) != len(uris):
        raise _fail("redirect_uris", "Duplicate redirect URI")
    if "authorization_code" in grants and not uris:
        raise _fail("redirect_uris", "At least one redirect URI is required for the authorization code flow")
    res = sorted(set(resources or ["api"]))
    if not set(res) <= {"api", "mcp"}:
        raise _fail("resources", "Unknown resource")
    if "mcp" in res and "authorization_code" not in grants:
        raise _fail("resources", "AI assistant tools (MCP) need the authorization code flow")
    catalogue = scope_catalogue()
    wanted = sorted(set(requested_scopes))
    if not wanted:
        raise _fail("requested_scopes", "Choose at least one scope")
    for key in wanted:
        if key not in catalogue:
            raise _fail("requested_scopes", f"Unknown scope {key}")
        if key.startswith("mcp:") and "mcp" not in res:
            raise _fail("requested_scopes", f"Scope {key} needs the AI assistant tools (MCP) resource")
    reasons: dict[str, str] = {}
    for key in wanted:
        reason = (scope_reasons.get(key) or "").strip()
        if not 10 <= len(reason) <= 300:
            raise _fail(f"scope_reasons.{key}", "Explain why the app needs this (10-300 characters)")
        reasons[key] = reason
    unknown_reasons = set(scope_reasons) - set(wanted)
    if unknown_reasons:
        raise _fail("scope_reasons", "A reason is given for a scope that is not requested")
    return grants, uris, res, wanted, reasons


def _manifest(data_manifest: dict[str, Any] | None, *, name: str, description: str | None) -> dict[str, Any]:
    try:
        m = ManifestIn.model_validate(data_manifest or {})
    except ValidationError as exc:
        raise _fail("manifest", str(exc.errors()[0].get("msg", "Invalid manifest"))[:300]) from exc
    return {"name": name, "description": description, **m.model_dump(mode="json")}


def manifest_digest(row_manifest: dict[str, Any], scopes_: list[str], resources: list[str]) -> str:
    view = {
        "scopes": sorted(scopes_),
        "resources": sorted(resources),
        "data_flows": row_manifest.get("data_flows", []),
        "events_consumed": sorted(row_manifest.get("events_consumed", [])),
    }
    blob = json.dumps(view, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _client_id() -> str:
    return "tosapp_" + base64.b32encode(secrets.token_bytes(20)).decode("ascii").lower()[:26]


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "app"
    return f"{base}-{secrets.token_hex(3)}"


def _new_secret(app_id: uuid.UUID, created_by: uuid.UUID | None) -> tuple[AppSecret, str]:
    plain = f"{SECRET_PREFIX}{secrets.token_urlsafe(32)}"
    row = AppSecret(
        id=uuid.uuid4(),
        app_id=app_id,
        secret_hash=hash_token(plain),
        prefix=plain[:12],
        fingerprint=plain[-4:],
        created_by_id=created_by,
    )
    return row, plain


# ================================================================ authorisation helpers
def _user_id(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    return principal.id if isinstance(principal, Principal) and principal.type == "user" else None


def _authorise_tenant(ctx: RequestContext) -> uuid.UUID:
    require_human(actor_from_ctx(ctx))
    principal = ctx.principal
    if not isinstance(principal, Principal) or ctx.tenant_id is None:
        raise NotFound("App not found")
    check(principal, "app:develop")
    return ctx.tenant_id


async def _load_for_manage(ctx: RequestContext, pdb: AsyncSession, app_id: uuid.UUID, *, lock: bool = True) -> App:
    """The App the caller may manage: tenant Apps through the owner tenant only (else 404); partner / platform Apps
    only from a non-tenant ctx (Epic 74 authorises the developer)."""
    stmt = select(App).where(App.id == app_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await pdb.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("App not found")
    if row.publisher_kind == "tenant":
        tenant_id = _authorise_tenant(ctx)
        if row.owner_tenant_id != tenant_id:
            raise NotFound("App not found")
    elif ctx.tenant_id is not None:
        raise NotFound("App not found")
    return row


# ================================================================ create / update
async def create_app(
    ctx: RequestContext, db: AsyncSession, data: AppCreate, *, publisher: Publisher
) -> AppCreated:
    s = get_settings()
    name = data.name.strip()
    if not 3 <= len(name) <= 120:
        raise _fail("name", "Enter a name of 3-120 characters")
    owner_tenant_id: uuid.UUID | None = None
    partner_org_id: uuid.UUID | None = None
    created_by_kind = publisher.created_by_kind
    created_by = publisher.created_by_id
    if publisher.kind == "tenant":
        owner_tenant_id = _authorise_tenant(ctx)
        if publisher.tenant_id is not None and publisher.tenant_id != owner_tenant_id:
            raise NotFound("Tenant not found")
        created_by_kind, created_by = "tenant_user", _user_id(ctx)
    elif publisher.kind == "partner":
        if publisher.partner_org_id is None:
            raise _fail("partner_org_id", "A partner organisation is required")
        partner_org_id = publisher.partner_org_id
    grants, uris, res, wanted, reasons = validate_registration(
        client_type=data.client_type,
        grant_types=list(data.grant_types),
        redirect_uris=list(data.redirect_uris),
        resources=list(data.resources),
        requested_scopes=list(data.requested_scopes),
        scope_reasons=dict(data.scope_reasons),
    )
    homepage = validate_https_url("homepage_url", data.homepage_url)
    privacy = validate_https_url("privacy_url", data.privacy_url)
    manifest = _manifest(data.manifest, name=name, description=data.description)
    async with platform_session() as pdb:
        if partner_org_id is not None:
            org = await pdb.get(PartnerOrg, partner_org_id)
            if org is None or org.status != "active":
                raise ConflictError("The partner organisation is not active", code="partner_inactive")
        if owner_tenant_id is not None:
            count = (
                await pdb.execute(
                    select(func.count()).where(
                        App.owner_tenant_id == owner_tenant_id, App.status != "retired"
                    )
                )
            ).scalar_one()
            if count >= s.private_apps_per_tenant:
                raise ConflictError(
                    f"Your organisation can have at most {s.private_apps_per_tenant} private apps",
                    code="private_app_limit",
                )
        row = App(
            id=uuid.uuid4(), client_id=_client_id(), name=name, slug=_slug(name), publisher_kind=publisher.kind,
            partner_org_id=partner_org_id, owner_tenant_id=owner_tenant_id, client_type=data.client_type,
            grant_types=grants, redirect_uris=uris, resources=res, requested_scopes=wanted, scope_reasons=reasons,
            manifest=manifest, manifest_version=1, manifest_sha256=manifest_digest(manifest, wanted, res),
            status="development", homepage_url=homepage, privacy_url=privacy,
            support_email=(data.support_email or None), created_by_kind=created_by_kind, created_by_id=created_by,
        )  # fmt: skip
        pdb.add(row)
        await pdb.flush()
        plain: str | None = None
        if data.client_type == "confidential":
            secret, plain = _new_secret(row.id, created_by)
            pdb.add(secret)
        await pdb.flush()
        await pdb.refresh(row)
        out = app_out(row)
    audit.record(
        "app.created", "app", out.id, session=db, client_id=out.client_id, name=out.name,
        publisher_kind=publisher.kind, scopes=wanted, resources=res, grant_types=grants, redirect_uris=uris,
    )  # fmt: skip
    return AppCreated(**out.model_dump(), client_secret=plain)


CONSENT_FIELDS = frozenset({"requested_scopes", "resources", "manifest"})


async def update_app(ctx: RequestContext, db: AsyncSession, app_id: uuid.UUID, data: AppUpdate) -> AppOut:
    provided = data.model_fields_set
    async with platform_session() as pdb:
        row = await _load_for_manage(ctx, pdb, app_id)
        if row.status == "retired":
            raise ConflictError("This app is retired", code="app_retired")
        before = {"name": row.name, "redirect_uris": list(row.redirect_uris or []),
                  "requested_scopes": list(row.requested_scopes), "resources": list(row.resources)}  # fmt: skip
        manifest = dict(row.manifest or {})
        if "name" in provided and data.name is not None:
            name = data.name.strip()
            if not 3 <= len(name) <= 120:
                raise _fail("name", "Enter a name of 3-120 characters")
            row.name = name
            manifest["name"] = name
        if "description" in provided:
            manifest["description"] = data.description
        if "homepage_url" in provided:
            row.homepage_url = validate_https_url("homepage_url", data.homepage_url)
        if "privacy_url" in provided:
            row.privacy_url = validate_https_url("privacy_url", data.privacy_url)
        if "support_email" in provided:
            row.support_email = data.support_email or None
        scopes_ = list(data.requested_scopes) if data.requested_scopes is not None else list(row.requested_scopes)
        reasons = dict(data.scope_reasons) if data.scope_reasons is not None else {
            k: v for k, v in dict(row.scope_reasons or {}).items() if k in scopes_
        }
        _, uris, res, wanted, reasons = validate_registration(
            client_type=row.client_type,
            grant_types=list(row.grant_types),
            redirect_uris=list(data.redirect_uris) if data.redirect_uris is not None else list(row.redirect_uris or []),
            resources=list(data.resources) if data.resources is not None else list(row.resources),
            requested_scopes=scopes_,
            scope_reasons=reasons,
        )
        if data.manifest is not None:
            manifest = {**manifest, **_manifest(data.manifest, name=row.name, description=manifest.get("description"))}
        row.redirect_uris, row.resources, row.requested_scopes, row.scope_reasons = uris, res, wanted, reasons
        digest = manifest_digest(manifest, wanted, res)
        added = (set(wanted) - set(before["requested_scopes"])) or (set(res) - set(before["resources"]))
        bump = digest != row.manifest_sha256 and (bool(added) or data.manifest is not None)
        if digest != row.manifest_sha256:
            row.manifest_sha256 = digest
        if bump:
            row.manifest_version += 1
        row.manifest = manifest
        await pdb.flush()
        await pdb.refresh(row)
        app_id_, version, owner = row.id, row.manifest_version, row.owner_tenant_id
        out = app_out(row)
    reconsent = 0
    if bump and owner is not None and owner == ctx.tenant_id:
        insts = list(
            (
                await db.execute(
                    select(AppInstallation).where(
                        AppInstallation.app_id == app_id_, AppInstallation.status != "uninstalled"
                    )
                )
            ).scalars()
        )
        for inst in insts:
            inst.pending_manifest_version = version
            emit(db, InstallationChanged(
                tenant_id=inst.tenant_id, installation_id=inst.id, kind=inst.kind, app_id=inst.app_id,
                connector_key=None, change="reconsent_required", status=inst.status,
                actor_kind=actor_from_ctx(ctx).kind,
            ))  # fmt: skip
        reconsent = len(insts)
        await db.flush()
    audit.record(
        "app.updated", "app", app_id_, session=db, before=before,
        after={"name": out.name, "redirect_uris": out.redirect_uris, "requested_scopes": out.requested_scopes,
               "resources": out.resources},
        manifest_version=version,
    )  # fmt: skip
    clear_cache()
    return out.model_copy(update={"reconsent_installations": reconsent})


# ================================================================ secrets
def _valid_secret(row: AppSecret, now: datetime) -> bool:
    return row.revoked_at is None and (row.expires_at is None or row.expires_at > now)


async def rotate_secret(ctx: RequestContext, db: AsyncSession, app_id: uuid.UUID) -> SecretCreated:
    s = get_settings()
    now = _now()
    async with platform_session() as pdb:
        app = await _load_for_manage(ctx, pdb, app_id)
        if app.client_type != "confidential":
            raise ConflictError("Public clients have no secret", code="public_client")
        if app.status == "retired":
            raise ConflictError("This app is retired", code="app_retired")
        live = [
            r
            for r in (
                await pdb.execute(
                    select(AppSecret).where(AppSecret.app_id == app_id, AppSecret.revoked_at.is_(None)).with_for_update()
                )
            ).scalars()
            if _valid_secret(r, now)
        ]
        if len(live) >= 2:
            raise ConflictError("Revoke the older secret first", code="secret_limit")
        previous_expires: datetime | None = None
        overlap = now + timedelta(hours=s.oauth_secret_overlap_hours)
        for r in live:
            r.expires_at = min(r.expires_at or overlap, overlap)
            previous_expires = r.expires_at
        secret, plain = _new_secret(app_id, _user_id(ctx))
        pdb.add(secret)
        await pdb.flush()
        out = SecretCreated(
            secret_id=secret.id, client_secret=plain, fingerprint=secret.fingerprint, prefix=secret.prefix,
            previous_expires_at=previous_expires,
        )  # fmt: skip
    audit.record("app.secret_rotated", "app", app_id, session=db, secret_id=out.secret_id,
                 previous_expires_at=previous_expires)  # fmt: skip
    return out


async def revoke_secret(
    ctx: RequestContext, db: AsyncSession, app_id: uuid.UUID, secret_id: uuid.UUID, *, confirm_last: bool = False
) -> None:
    now = _now()
    async with platform_session() as pdb:
        await _load_for_manage(ctx, pdb, app_id)
        rows = list(
            (await pdb.execute(select(AppSecret).where(AppSecret.app_id == app_id).with_for_update())).scalars()
        )
        target = next((r for r in rows if r.id == secret_id), None)
        if target is None:
            raise NotFound("Secret not found")
        if target.revoked_at is not None:
            return
        others = [r for r in rows if r.id != secret_id and _valid_secret(r, now)]
        if not others and not confirm_last:
            raise ConflictError("This is the app's last valid secret. The app will stop working.", code="last_secret")
        target.revoked_at = now
    audit.record("app.secret_revoked", "app", app_id, session=db, secret_id=secret_id)


async def list_secrets(ctx: RequestContext, app_id: uuid.UUID) -> list[dict[str, Any]]:
    async with platform_session() as pdb:
        await _load_for_manage_read(ctx, pdb, app_id)
        rows = (
            await pdb.execute(select(AppSecret).where(AppSecret.app_id == app_id).order_by(AppSecret.created_at.desc()))
        ).scalars()
        return [
            {"id": r.id, "prefix": r.prefix, "fingerprint": r.fingerprint, "created_at": r.created_at,
             "expires_at": r.expires_at, "last_used_at": r.last_used_at, "revoked_at": r.revoked_at}
            for r in rows
        ]  # fmt: skip


# ================================================================ retire / reads
async def retire_app(ctx: RequestContext, db: AsyncSession, app_id: uuid.UUID, *, reason: str) -> AppOut:
    async with platform_session() as pdb:
        row = await _load_for_manage(ctx, pdb, app_id, lock=False)
        if row.publisher_kind != "tenant":
            raise NotFound("App not found")
    out = await set_app_status(ctx, db, app_id, "retired", reason)
    audit.record("app.retired", "app", app_id, session=db, reason=reason[:500])
    return out


async def _load_for_manage_read(ctx: RequestContext, pdb: AsyncSession, app_id: uuid.UUID) -> App:
    principal = ctx.principal
    if not isinstance(principal, Principal) or ctx.tenant_id is None:
        raise NotFound("App not found")
    if not (principal.has("app:develop") or principal.has("integration:view")):
        check(principal, "app:develop")
    row = await pdb.get(App, app_id)
    if row is None or row.publisher_kind != "tenant" or row.owner_tenant_id != ctx.tenant_id:
        raise NotFound("App not found")
    return row


async def list_private_apps(ctx: RequestContext, db: AsyncSession) -> list[AppOut]:  # ruff: ignore[unused-function-argument]
    principal = ctx.principal
    if not isinstance(principal, Principal) or ctx.tenant_id is None:
        return []
    if not (principal.has("app:develop") or principal.has("integration:view")):
        check(principal, "app:develop")
    async with platform_session() as pdb:
        rows = (
            await pdb.execute(
                select(App).where(App.publisher_kind == "tenant", App.owner_tenant_id == ctx.tenant_id).order_by(App.created_at.desc())
            )
        ).scalars()
        return [app_out(r) for r in rows]


async def get_private_app(ctx: RequestContext, db: AsyncSession, app_id: uuid.UUID) -> AppOut:  # ruff: ignore[unused-function-argument]
    async with platform_session() as pdb:
        return app_out(await _load_for_manage_read(ctx, pdb, app_id))


async def set_app_status(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    app_id: uuid.UUID,
    status: AppStatus,
    reason: str | None,
) -> AppOut:
    """Platform-side App runtime status (69 review, 79 ops). Suspended / retired: the caller's tenant tokens are revoked
    in ``db`` (when it is a tenant session); every other tenant's tokens fail at once through ``is_suspended``."""
    if status not in APP_STATUSES:
        raise ValidationFailed("Unknown app status", details={"fields": {"status": " | ".join(APP_STATUSES)}})
    async with platform_session() as pdb:
        row = (await pdb.execute(select(App).where(App.id == app_id).with_for_update())).scalar_one_or_none()
        if row is None:
            raise NotFound("App not found")
        row.status = status
        row.suspended_reason = (reason or None) if status == "suspended" else None
        audit.record("oauth.app_status", "app", app_id, session=pdb, status=status)
        await pdb.flush()
        out = app_out(row)
    if status in ("suspended", "retired") and db.sync_session.info.get("tenant_id"):
        await revoke_for(db, app_id=app_id, reason="suspended")
    ecosystem_service.bust_cache()
    clear_cache()
    return out


# ================================================================ Profile -> Connected apps / usage helpers
async def list_connected_apps(ctx: RequestContext, db: AsyncSession) -> list[dict[str, Any]]:
    """The caller's live delegated authorisations with App name, publisher and last use (spec A6)."""
    from app.models.identity import Tenant
    from app.models.oauth import OAuthToken
    from app.services.oauth.authorizations import list_my_authorizations

    auths = await list_my_authorizations(ctx, db)
    if not auths:
        return []
    last_used = dict(
        (
            await db.execute(
                select(OAuthToken.user_authorization_id, func.max(OAuthToken.last_used_at))
                .where(OAuthToken.user_authorization_id.in_([a.id for a in auths]))
                .group_by(OAuthToken.user_authorization_id)
            )
        ).tuples().all()
    )
    app_ids = {a.app_id for a in auths if a.app_id}
    publishers: dict[uuid.UUID, tuple[str, str]] = {}
    async with platform_session() as pdb:
        for app in (await pdb.execute(select(App).where(App.id.in_(app_ids)))).scalars():
            if app.publisher_kind == "platform":
                name = "Talent OS"
            elif app.publisher_kind == "partner" and app.partner_org_id:
                org = await pdb.get(PartnerOrg, app.partner_org_id)
                name = org.name if org else "Partner"
            else:
                tenant = await pdb.get(Tenant, app.owner_tenant_id) if app.owner_tenant_id else None
                name = tenant.name if tenant else "Your organisation"
            publishers[app.id] = (app.publisher_kind, name)
    catalogue = scope_catalogue()
    return [
        {"id": a.id, "installation_id": a.installation_id, "app_id": a.app_id, "app_name": a.app_name,
         "publisher_kind": publishers.get(a.app_id, ("tenant", ""))[0] if a.app_id else "tenant",
         "publisher_name": publishers.get(a.app_id, ("", ""))[1] if a.app_id else "",
         "resource": a.resource, "scopes": a.scopes,
         "scope_labels": [catalogue[k].label if k in catalogue else k for k in a.scopes],
         "granted_at": a.granted_at, "last_used_at": last_used.get(a.id)}
        for a in auths
    ]  # fmt: skip


async def installation_for_app(ctx: RequestContext, db: AsyncSession, app_id: uuid.UUID) -> uuid.UUID | None:
    """The tenant's live Installation of one of its private Apps (usage reads)."""
    async with platform_session() as pdb:
        await _load_for_manage_read(ctx, pdb, app_id)
    return (
        await db.execute(
            select(AppInstallation.id).where(AppInstallation.app_id == app_id, AppInstallation.status != "uninstalled")
        )
    ).scalar_one_or_none()


def scope_catalogue_out() -> list[dict[str, Any]]:
    return [
        {"key": k, "label": s.label, "description": s.description, "sensitive": s.sensitive,
         "resources": sorted(s.resources), "api_key_allowed": s.api_key_allowed}
        for k, s in sorted(scope_catalogue().items())
    ]  # fmt: skip


__all__ = [
    "app_out",
    "create_app",
    "get_private_app",
    "installation_for_app",
    "list_connected_apps",
    "list_private_apps",
    "list_secrets",
    "manifest_digest",
    "retire_app",
    "revoke_secret",
    "rotate_secret",
    "scope_catalogue_out",
    "set_app_status",
    "update_app",
    "validate_redirect_uri",
    "validate_registration",
]
