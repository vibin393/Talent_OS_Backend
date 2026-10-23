# ruff: file-ignore[line-too-long, non-imperative-mood, class-as-data-structure, unused-function-argument]
"""Install contract (Epic 66 minimal body, R-2; Epic 69 fills the bodies with the SAME signatures, spec A4).

* ``install`` (★ ``require_human`` + ``integration:manage``): availability (``app_unavailable`` / ``app_private``), the
  Tenant app policy (``publisher_not_allowed`` / ``private_apps_disabled``), consent freshness (``manifest_changed``),
  scopes and sensitive purposes (``purpose_required``), private-App automated checks (``app_checks_failed``), connector
  config / secrets / Legal Entities / accountable user, a Sandbox-Tenant real-adapter refusal, the live-mode guard
  (D-22, Epic 79), the sensitive-scope approval chain (``pending_approval``) and the test-gated activation of
  connectors (pass -> ``active``; fail -> stays ``pending_consent`` with ``test={ok: false, ...}``). Apps -> ``active``.
* ``upgrade`` (★): re-consent to the current manifest (new ``app_grants`` row; ``reconsent_required`` -> ``active``).
* ``configure``: config / secrets (vault) / Legal Entities (add -> config row; remove -> config disabled, its jobs dead)
  / accountable user; audit with secret names only; a config change re-runs the test (``trigger='manual'``).
* ``set_status``: ``suspended`` revokes tokens; resuming is ★ and refused while a kill switch / platform suspension is on.
* ``uninstall``: optional erasure propagation first, then revoke tokens, delete secrets, disable configs + sync state,
  queued jobs dead, ``uninstalled``. History is kept. Works even when the adapter fails or the kill switch is on.
* ``list_installations``: filter ``status`` / ``kind`` / ``category``; health, last sync, pending approval.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.database import platform_session
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.oauth_scopes import is_sensitive, scopes
from app.core.permissions import check
from app.core.public_api import PublicPage, decode_cursor, encode_cursor
from app.core.tenancy import is_sandbox
from app.events.ecosystem import InstallationChanged
from app.models.connectors import MULTI_PROVIDER_CATEGORIES, ConnectorHookLookup, ProviderConnectorConfig
from app.models.integrations import ConnectorSyncState
from app.models.oauth import App, AppGrant, AppInstallation
from app.schemas.marketplace import (
    ConfigureIn,
    InstallationOut,
    InstallationQuery,
    InstallIn,
    SecretMetaOut,
    TestResultOut,
    UserAuthorizationsIn,
)
from app.services import ecosystem_service, oauth_service, vault_service
from app.services.marketplace import approvals as install_approvals
from app.services.marketplace import health, notify, platform_catalog
from app.services.marketplace import policy as app_policy
from app.services.marketplace import uninstall as uninstall_extras
from app.services.marketplace.legacy_catalog import listing_category

# (ctx, db, installation_id | None, connector_key, new_mode) -> raise to refuse (Epic 79 burn-in guard, D-22).
LiveModeGuard = Callable[[RequestContext, AsyncSession, uuid.UUID | None, str, str], Awaitable[None]]
_live_mode_guards: list[LiveModeGuard] = []


def register_live_mode_guard(fn: LiveModeGuard) -> None:
    """Additive hook (D-22): called when a connector is installed or configured in ``live`` mode."""
    if fn not in _live_mode_guards:
        _live_mode_guards.append(fn)


def unregister_live_mode_guard(fn: LiveModeGuard) -> None:
    if fn in _live_mode_guards:
        _live_mode_guards.remove(fn)


def _now() -> datetime:
    return datetime.now(UTC)


def _tenant(ctx: RequestContext) -> uuid.UUID:
    assert ctx.tenant_id is not None
    return ctx.tenant_id


def _user_id(ctx: RequestContext) -> uuid.UUID:
    pid = getattr(ctx.principal, "id", None)
    assert pid is not None
    return pid  # type: ignore[no-any-return]


def _require_grant(ctx: RequestContext) -> None:
    require_human(actor_from_ctx(ctx))
    if ctx.principal is not None:
        check(ctx.principal, "integration:manage")


def _require_manage(ctx: RequestContext) -> None:
    if ctx.principal is not None:
        check(ctx.principal, "integration:manage")


def manifest_digest(manifest: Any) -> str:
    """The consented connector manifest digest (``core.integrations.manifest_sha256``)."""
    from app.core.integrations import manifest_sha256

    return manifest_sha256(manifest)


def _connector_manifest(key: str) -> Any:
    from app.core import integrations

    try:
        return integrations.manifest(key)
    except KeyError as exc:
        raise NotFound("Connector not found", code="listing_not_found") from exc


def _out(row: AppInstallation, config_ids: list[uuid.UUID] | None = None, **extra: Any) -> InstallationOut:
    category = None
    if row.kind == "connector":
        from app.core.integrations import find_manifest

        m = find_manifest(str(row.connector_key))
        category = listing_category(m.category) if m is not None else None
    return InstallationOut(
        id=row.id,
        kind=row.kind,
        app_id=row.app_id,
        connector_key=row.connector_key,
        instance_no=row.instance_no,
        display_name=row.display_name,
        status=row.status,
        manifest_version=row.manifest_version,
        manifest_sha256=row.manifest_sha256,
        pending_manifest_version=row.pending_manifest_version,
        granted_scopes=list(row.granted_scopes or []),
        sensitive_purposes=dict(row.sensitive_purposes or {}),
        granted_by=row.granted_by,
        granted_at=row.granted_at,
        accountable_user_id=row.accountable_user_id,
        config=dict(row.config or {}),
        legal_entity_ids=list(row.legal_entity_ids or []),
        health_status=row.health_status,
        last_error_code=row.last_error_code,
        config_ids=config_ids or [],
        created_at=row.created_at,
        suspended_reason=row.suspended_reason,
        health_checked_at=row.health_checked_at,
        category=extra.pop("category", None) or category,
        **extra,
    )


async def _config_ids(
    db: AsyncSession, installation_id: uuid.UUID, *, active_only: bool = False
) -> list[uuid.UUID]:
    stmt = select(ProviderConnectorConfig.id).where(
        ProviderConnectorConfig.installation_id == installation_id
    )
    if active_only:
        stmt = stmt.where(ProviderConnectorConfig.status == "active")
    return list((await db.execute(stmt.order_by(ProviderConnectorConfig.created_at))).scalars())


async def secret_meta(ctx: RequestContext, db: AsyncSession, inst: AppInstallation) -> list[SecretMetaOut]:
    """Names, ``has_secret``, last-4 fingerprint and rotation date only (never a value)."""
    ids = await _config_ids(db, inst.id, active_only=True)
    seen: dict[str, SecretMetaOut] = {}
    for cid in ids[:1]:
        for m in await vault_service.meta(ctx, db, "provider_config", cid):
            if m.name.endswith(vault_service.PREVIOUS_SUFFIX):
                continue
            seen[m.name] = SecretMetaOut(
                name=m.name, has_secret=m.has_secret, fingerprint=m.fingerprint, rotated_at=m.rotated_at
            )
    if inst.kind == "connector":
        from app.core.integrations import find_manifest

        manifest = find_manifest(str(inst.connector_key))
        for f in manifest.secret_fields if manifest is not None else ():
            seen.setdefault(f.name, SecretMetaOut(name=f.name, has_secret=False))
    return sorted(seen.values(), key=lambda s: s.name)


async def detail(
    ctx: RequestContext, db: AsyncSession, inst: AppInstallation, **extra: Any
) -> InstallationOut:
    last_sync = (
        await db.execute(
            select(func.max(ConnectorSyncState.last_success_at)).where(
                ConnectorSyncState.installation_id == inst.id
            )
        )
    ).scalar_one_or_none()
    pending = (
        await install_approvals.pending_request_id(db, inst.id) if inst.status == "pending_approval" else None
    )
    return _out(
        inst, await _config_ids(db, inst.id), secrets=await secret_meta(ctx, db, inst), last_sync_at=last_sync,
        approval_request_id=pending, **extra,
    )  # fmt: skip


def _check_scopes(requested: list[str], allowed: list[str], purposes: dict[str, str]) -> list[str]:
    catalogue = scopes()
    out = sorted(set(requested))
    fields: dict[str, str] = {}
    for key in out:
        if key not in catalogue:
            fields[key] = "Unknown scope"
        elif key not in allowed:
            fields[key] = "The app does not request this scope"
    if fields:
        raise ValidationFailed("Invalid scopes", code="invalid_scope", details={"fields": fields})
    missing = {
        k: "Explain why your organisation needs this (10-500 characters)"
        for k in out
        if is_sensitive(k) and not 10 <= len((purposes.get(k) or "").strip()) <= 500
    }
    if missing:
        raise ValidationFailed(
            "A purpose is required for each sensitive scope",
            code="purpose_required",
            details={"fields": missing},
        )
    return out


async def _grant_row(
    db: AsyncSession,
    inst: AppInstallation,
    *,
    scopes_: list[str],
    purposes: dict[str, str],
    flows: Any,
    user_id: uuid.UUID,
) -> None:
    now = _now()
    await db.execute(
        update(AppGrant)
        .where(AppGrant.installation_id == inst.id, AppGrant.superseded_at.is_(None))
        .values(superseded_at=now)
    )
    db.add(
        AppGrant(
            id=uuid.uuid4(),
            tenant_id=inst.tenant_id,
            installation_id=inst.id,
            manifest_version=inst.manifest_version,
            manifest_sha256=inst.manifest_sha256,
            scopes=scopes_,
            sensitive_purposes=purposes,
            data_flows=flows,
            granted_by=user_id,
            granted_at=now,
        )
    )


async def _app_for_install(tenant_id: uuid.UUID, app_id: uuid.UUID) -> App:
    async with platform_session() as pdb:
        app = await pdb.get(App, app_id)
    if app is None:
        raise NotFound("App not found", code="listing_not_found")
    if app.status in ("suspended", "retired"):
        raise ConflictError("This app is unavailable", code="app_unavailable")
    if app.publisher_kind == "tenant" and app.owner_tenant_id != tenant_id:
        raise ConflictError("This app is private to another workspace", code="app_private")
    if app.status == "development" and app.publisher_kind != "tenant":
        sandbox = await is_sandbox(tenant_id)
        owner_ok = False
        if sandbox and app.partner_org_id is not None:
            async with platform_session() as pdb:
                from app.models.identity import Tenant

                t = await pdb.get(Tenant, tenant_id)
                owner_ok = (
                    t is not None
                    and t.sandbox_owner_kind == "partner_org"
                    and t.sandbox_owner_id == app.partner_org_id
                )
        if not owner_ok:
            raise ConflictError("This app is still in development", code="app_unavailable")
    return app


async def _check_accountable(db: AsyncSession, user_id: uuid.UUID | None) -> uuid.UUID:
    from sqlalchemy import text

    if user_id is None:
        raise ValidationFailed(
            "An accountable person is required", details={"fields": {"accountable_user_id": "Required"}}
        )
    row = (
        await db.execute(
            text(
                "SELECT 1 FROM users u WHERE u.id = :u AND u.is_active AND (EXISTS (SELECT 1 FROM user_roles ur JOIN roles r "
                "ON r.id = ur.role_id WHERE ur.user_id = u.id AND (r.key = 'tenant_admin' OR r.permissions ? 'integration:view' "
                "OR r.permissions ? 'integration:manage')))"
            ),
            {"u": user_id},
        )
    ).first()
    if row is None:
        raise ValidationFailed(
            "The accountable person must be an active user who can view integrations",
            details={"fields": {"accountable_user_id": "Not eligible"}},
        )
    return user_id


async def _check_legal_entities(db: AsyncSession, manifest: Any, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    from app.models.legal_entities import LegalEntity

    if not manifest.legal_entity_scoped:
        return list(ids)
    if not ids:
        # A Tenant without any active Legal Entity installs tenant-wide (one config row with no Legal Entity).
        any_entity = (
            await db.execute(select(LegalEntity.id).where(LegalEntity.status == "active").limit(1))
        ).first()
        if any_entity is None:
            return []
        raise ValidationFailed(
            "Choose at least one legal entity",
            code="legal_entity_required",
            details={"fields": {"legal_entity_ids": "Required"}},
        )
    found = set(
        (
            await db.execute(
                select(LegalEntity.id).where(LegalEntity.id.in_(ids), LegalEntity.status == "active")
            )
        ).scalars()
    )
    if found != set(ids):
        raise ValidationFailed(
            "Each legal entity must be active",
            code="legal_entity_required",
            details={"fields": {"legal_entity_ids": "Not active"}},
        )
    return list(dict.fromkeys(ids))


def _validate_config(manifest: Any, raw: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    data = dict(raw or {})
    mode = data.pop("mode", None)
    if mode is not None and mode not in manifest.adapters:
        raise ValidationFailed(
            "Unknown adapter mode", code="invalid_config", details={"fields": {"mode": "Unknown"}}
        )
    try:
        config = manifest.config_model.model_validate(data).model_dump(mode="json")
    except Exception as exc:
        fields: dict[str, str] = {}
        for err in getattr(exc, "errors", lambda: [])():
            loc = ".".join(str(p) for p in err.get("loc", ())) or "config"
            fields[loc] = str(err.get("msg", "Invalid"))[:200]
        raise ValidationFailed(
            "Invalid configuration",
            code="invalid_config",
            details={"fields": fields or {"config": "Invalid"}},
        ) from exc
    return config, mode


async def _check_mode(
    ctx: RequestContext,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    manifest: Any,
    mode: str | None,
    installation_id: uuid.UUID | None,
) -> None:
    from app.core.integrations import resolve_mode

    sandbox = await is_sandbox(tenant_id)
    if sandbox and mode is not None and mode not in ("fake", "manual"):
        raise ValidationFailed(
            "Sandbox tenants can use only Fake or Manual connectors",
            code="sandbox_real_adapter",
            details={"provider_key": manifest.key},
        )
    effective = resolve_mode(manifest, mode, sandbox=sandbox)
    if effective == "live":
        for guard in list(_live_mode_guards):
            await guard(ctx, db, installation_id, manifest.key, effective)


async def install(ctx: RequestContext, db: AsyncSession, data: InstallIn) -> InstallationOut:
    _require_grant(ctx)
    tenant_id, user_id = _tenant(ctx), _user_id(ctx)
    policy = await app_policy.current(db, tenant_id)
    purposes = {k: v.strip()[:500] for k, v in data.sensitive_purposes.items() if v and v.strip()}
    listing_name: str
    if data.kind == "app":
        if data.app_id is None:
            raise ValidationFailed("app_id is required", details={"fields": {"app_id": "Required"}})
        app = await _app_for_install(tenant_id, data.app_id)
        app_policy.check_install(policy, app.publisher_kind)
        if data.accept_manifest_sha256 != app.manifest_sha256:
            raise ConflictError("The app changed since you reviewed it", code="manifest_changed")
        granted = _check_scopes(data.scopes, list(app.requested_scopes), purposes)
        if app.publisher_kind == "tenant":
            from app.services.marketplace.checks import run_manifest_checks

            failed = [c.as_dict() for c in run_manifest_checks(app, kind="private") if not c.passed]
            if failed:
                raise ValidationFailed(
                    "The app failed automated checks", code="app_checks_failed", details={"checks": failed}
                )
        existing = (
            await db.execute(
                select(AppInstallation).where(
                    AppInstallation.kind == "app",
                    AppInstallation.app_id == app.id,
                    AppInstallation.status != "uninstalled",
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ConflictError("This app is already installed", code="already_installed")
        listing_name = app.name
        inst = AppInstallation(
            id=uuid.uuid4(), tenant_id=tenant_id, kind="app", app_id=app.id, display_name=(data.display_name or app.name)[:200],
            manifest_version=app.manifest_version, manifest_sha256=app.manifest_sha256, status="pending_consent",
            granted_scopes=granted, sensitive_purposes={k: purposes[k] for k in granted if k in purposes},
            granted_by=user_id, granted_at=_now(), accountable_user_id=data.accountable_user_id or user_id,
            config=data.config, legal_entity_ids=data.legal_entity_ids,
        )  # fmt: skip
        db.add(inst)
        await db.flush()
        await _grant_row(
            db, inst, scopes_=granted, purposes=inst.sensitive_purposes,
            flows=(app.manifest or {}).get("data_flows", []), user_id=user_id,
        )  # fmt: skip
        config_ids: list[uuid.UUID] = []
    else:
        if not data.connector_key:
            raise ValidationFailed(
                "connector_key is required", details={"fields": {"connector_key": "Required"}}
            )
        manifest = _connector_manifest(data.connector_key)
        app_policy.check_install(policy, "platform")
        digest = manifest_digest(manifest)
        if data.accept_manifest_sha256 != digest:
            raise ConflictError("The connector changed since you reviewed it", code="manifest_changed")
        config, mode = _validate_config(manifest, data.config)
        secret_names = {s.name for s in manifest.secret_fields}
        unknown = sorted(set(data.secrets) - secret_names)
        if unknown:
            raise ValidationFailed(
                "Unknown secret fields",
                code="invalid_config",
                details={"fields": dict.fromkeys(unknown, "Unknown")},
            )
        missing = sorted(
            s.name
            for s in manifest.secret_fields
            if s.required
            and s.kind != "oauth_refresh"
            and not (data.secrets.get(s.name) and data.secrets[s.name].get_secret_value())
        )
        if missing:
            raise ValidationFailed(
                "Required secrets are missing", code="secret_required", details={"names": missing}
            )
        accountable = await _check_accountable(db, data.accountable_user_id)
        entities = await _check_legal_entities(db, manifest, list(data.legal_entity_ids))
        await _check_mode(ctx, db, tenant_id, manifest, mode, None)
        if not manifest.multi_install:
            live = (
                await db.execute(
                    select(func.count()).where(
                        AppInstallation.kind == "connector",
                        AppInstallation.connector_key == manifest.key,
                        AppInstallation.status != "uninstalled",
                    )
                )
            ).scalar_one()
            if live:
                raise ConflictError("This connector is already installed", code="already_installed")
        instance_no = (
            await db.execute(
                select(func.coalesce(func.max(AppInstallation.instance_no), 0)).where(
                    AppInstallation.kind == "connector", AppInstallation.connector_key == manifest.key
                )
            )
        ).scalar_one() + 1
        await platform_catalog.ensure_version(manifest)
        listing_name = manifest.name
        inst = AppInstallation(
            id=uuid.uuid4(), tenant_id=tenant_id, kind="connector", connector_key=manifest.key, instance_no=instance_no,
            display_name=(data.display_name or manifest.name)[:200], manifest_version=manifest.version,
            manifest_sha256=digest, status="pending_consent", granted_scopes=[], sensitive_purposes={}, granted_by=user_id,
            granted_at=_now(), accountable_user_id=accountable, config=config, legal_entity_ids=entities,
        )  # fmt: skip
        db.add(inst)
        await db.flush()
        await _grant_row(
            db,
            inst,
            scopes_=[],
            purposes={},
            flows=[platform_catalog.flow_dict(f) for f in manifest.data_flows],
            user_id=user_id,
        )
        settings = {**config, **({"mode": mode} if mode else {})}
        config_ids = await _create_configs(ctx, db, inst, manifest, settings, entities, data.secrets)
    # Approval chain for sensitive scopes (spec A4 step 7).
    sensitive = [s for s in inst.granted_scopes if is_sensitive(s)]
    chain_id = policy.get("sensitive_approval_chain_id")
    test: dict[str, Any] | None = None
    approval_id: uuid.UUID | None = None
    change = "installed"
    if sensitive and chain_id:
        inst.status = "pending_approval"
        await db.flush()
        status, approval_id = await install_approvals.request(
            ctx, db, inst, chain_id=uuid.UUID(str(chain_id)), listing_name=listing_name, sensitive=sensitive
        )
        if status == "auto_approved":
            await install_approvals.on_final_decided(ctx, db, _AutoApproved(inst.id))
        elif approval_id is not None:
            await notify.send(db, "installation_pending_approval", {
                "installation_id": str(inst.id), "request_id": str(approval_id), "name": inst.display_name,
                "sensitive_scopes": sorted(sensitive),
            })  # fmt: skip
    elif inst.kind == "connector":
        outcome = await health.run_test(ctx, db, inst, trigger="install")
        test = {"ok": outcome.ok, "code": outcome.code, "message": outcome.message}
        if outcome.ok:
            inst.status = "active"
        else:
            await notify.send(db, "installation_test_failed", {
                "installation_id": str(inst.id), "user_id": str(user_id), "name": inst.display_name, "message": outcome.message,
            })  # fmt: skip
    else:
        inst.status = "active"
    audit.record("marketplace.installed", "app_installation", inst.id, session=db, kind=inst.kind,
                 app_id=str(inst.app_id) if inst.app_id else None, connector_key=inst.connector_key,
                 scopes=list(inst.granted_scopes), status=inst.status, secret_names=sorted(data.secrets))  # fmt: skip
    emit(db, InstallationChanged(
        tenant_id=tenant_id, installation_id=inst.id, kind=inst.kind, app_id=inst.app_id,
        connector_key=inst.connector_key, change=change, status=inst.status, actor_kind=actor_from_ctx(ctx).kind,
    ))  # fmt: skip
    await db.flush()
    await db.refresh(inst)
    return _out(inst, config_ids, test=test, approval_request_id=approval_id)


class _AutoApproved:
    outcome = "auto_approved"
    comment = None

    def __init__(self, subject_id: uuid.UUID) -> None:
        self.subject_id = subject_id


async def _create_configs(
    ctx: RequestContext,
    db: AsyncSession,
    inst: AppInstallation,
    manifest: Any,
    settings: dict[str, Any],
    entities: list[uuid.UUID],
    secrets: dict[str, Any],
) -> list[uuid.UUID]:
    targets: list[uuid.UUID | None] = list(entities) or [None]
    slot = manifest.key if manifest.category in MULTI_PROVIDER_CATEGORIES else ""
    ids: list[uuid.UUID] = []
    for entity in targets:
        row = ProviderConnectorConfig(
            id=uuid.uuid4(), tenant_id=inst.tenant_id, category=manifest.category, provider_key=manifest.key,
            legal_entity_id=entity, display_name=inst.display_name, settings=settings, status="active",
            installation_id=inst.id, slot_key=slot,
        )  # fmt: skip
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        except Exception as exc:
            raise ConflictError(
                "An active configuration already exists for this category and legal entity",
                code="connector_config_exists",
            ) from exc
        for name, value in secrets.items():
            plain = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
            if plain:
                await vault_service.put(
                    ctx, db, vault_service.SecretRef("provider_config", row.id, name), plain
                )
        ids.append(row.id)
    async with platform_session() as pdb:
        for cid in ids:
            await pdb.execute(
                pg_insert(ConnectorHookLookup)
                .values(config_id=cid, tenant_id=inst.tenant_id)
                .on_conflict_do_nothing()
            )
    return ids


async def _get(db: AsyncSession, installation_id: uuid.UUID) -> AppInstallation:
    row = (
        await db.execute(
            select(AppInstallation).where(AppInstallation.id == installation_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Installation not found")
    return row


async def get(ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID) -> InstallationOut:
    row = await db.get(AppInstallation, installation_id)
    if row is None:
        raise NotFound("Installation not found")
    return await detail(ctx, db, row)


async def _current_manifest(inst: AppInstallation) -> tuple[int, str, list[Any]]:
    if inst.kind == "app":
        async with platform_session() as pdb:
            app = await pdb.get(App, inst.app_id)
        if app is None:
            raise NotFound("App not found")
        return app.manifest_version, app.manifest_sha256, list((app.manifest or {}).get("data_flows", []))
    manifest = _connector_manifest(str(inst.connector_key))
    await platform_catalog.ensure_version(manifest)
    return (
        manifest.version,
        manifest_digest(manifest),
        [platform_catalog.flow_dict(f) for f in manifest.data_flows],
    )


async def upgrade(
    ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, *, accept_manifest_sha256: str
) -> InstallationOut:
    """Re-consent to the current manifest (★): a new ``app_grants`` row (the previous one superseded), the pending
    version cleared, ``reconsent_required`` -> ``active``. Scopes stay as granted (a wider grant is a new install)."""
    _require_grant(ctx)
    inst = await _get(db, installation_id)
    if inst.status == "uninstalled":
        raise ConflictError("This installation was removed", code="installation_uninstalled")
    version, digest, flows = await _current_manifest(inst)
    if accept_manifest_sha256 != digest:
        raise ConflictError("The manifest changed since you reviewed it", code="manifest_changed")
    inst.manifest_version, inst.manifest_sha256, inst.pending_manifest_version = version, digest, None
    if inst.status == "reconsent_required":
        inst.status = "active"
    inst.granted_by, inst.granted_at = _user_id(ctx), _now()
    await _grant_row(
        db, inst, scopes_=list(inst.granted_scopes), purposes=dict(inst.sensitive_purposes or {}), flows=flows,
        user_id=_user_id(ctx),
    )  # fmt: skip
    audit.record("marketplace.upgraded", "app_installation", inst.id, session=db, manifest_version=version)
    emit(db, InstallationChanged(
        tenant_id=inst.tenant_id, installation_id=inst.id, kind=inst.kind, app_id=inst.app_id,
        connector_key=inst.connector_key, change="upgraded", status=inst.status, actor_kind=actor_from_ctx(ctx).kind,
    ))  # fmt: skip
    await db.flush()
    return await detail(ctx, db, inst)


async def configure(
    ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, data: ConfigureIn
) -> InstallationOut:
    require_human(actor_from_ctx(ctx))
    _require_manage(ctx)
    inst = await _get(db, installation_id)
    if inst.status == "uninstalled":
        raise ConflictError("This installation was removed", code="installation_uninstalled")
    changed: list[str] = []
    before = {"config": dict(inst.config or {}), "legal_entity_ids": [str(x) for x in inst.legal_entity_ids or []],
              "accountable_user_id": str(inst.accountable_user_id) if inst.accountable_user_id else None,
              "display_name": inst.display_name}  # fmt: skip
    manifest = _connector_manifest(str(inst.connector_key)) if inst.kind == "connector" else None
    if data.config is not None:
        if manifest is not None:
            config, mode = _validate_config(manifest, data.config)
            old_mode = None
            first = (
                await db.execute(
                    select(ProviderConnectorConfig.settings)
                    .where(ProviderConnectorConfig.installation_id == inst.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if first:
                old_mode = dict(first).get("mode")
            if mode is not None and mode != old_mode:
                await _check_mode(ctx, db, inst.tenant_id, manifest, mode, inst.id)
            inst.config = config
            settings = {**config, **({"mode": mode} if mode else ({"mode": old_mode} if old_mode else {}))}
            await db.execute(
                update(ProviderConnectorConfig)
                .where(ProviderConnectorConfig.installation_id == inst.id)
                .values(settings=settings)
            )
        else:
            inst.config = data.config
        changed.append("config")
    if data.legal_entity_ids is not None and manifest is not None:
        wanted = await _check_legal_entities(db, manifest, list(data.legal_entity_ids))
        await _apply_legal_entities(ctx, db, inst, manifest, wanted)
        changed.append("legal_entity_ids")
    if data.secrets:
        if manifest is not None:
            unknown = sorted(set(data.secrets) - {s.name for s in manifest.secret_fields})
            if unknown:
                raise ValidationFailed(
                    "Unknown secret fields",
                    code="invalid_config",
                    details={"fields": dict.fromkeys(unknown, "Unknown")},
                )
        for cid in await _config_ids(db, inst.id, active_only=True):
            for name, value in data.secrets.items():
                ref = vault_service.SecretRef("provider_config", cid, name)
                existing = {m.name for m in await vault_service.meta(ctx, db, "provider_config", cid)}
                if name in existing:
                    await vault_service.rotate(ctx, db, ref, value.get_secret_value())
                else:
                    await vault_service.put(ctx, db, ref, value.get_secret_value())
        changed.append("secrets")
    if data.accountable_user_id is not None:
        inst.accountable_user_id = await _check_accountable(db, data.accountable_user_id)
        changed.append("accountable_user_id")
    if data.display_name:
        inst.display_name = data.display_name[:200]
        changed.append("display_name")
    after = {"config": dict(inst.config or {}), "legal_entity_ids": [str(x) for x in inst.legal_entity_ids or []],
             "accountable_user_id": str(inst.accountable_user_id) if inst.accountable_user_id else None,
             "display_name": inst.display_name}  # fmt: skip
    audit.record("marketplace.configured", "app_installation", inst.id, session=db, changed=changed,
                 before=before, after=after, secret_names=sorted(data.secrets or {}))  # fmt: skip
    emit(db, InstallationChanged(
        tenant_id=inst.tenant_id, installation_id=inst.id, kind=inst.kind, app_id=inst.app_id,
        connector_key=inst.connector_key, change="config_changed", status=inst.status, actor_kind=actor_from_ctx(ctx).kind,
    ))  # fmt: skip
    await db.flush()
    if (
        manifest is not None
        and {"config", "secrets", "legal_entity_ids"} & set(changed)
        and inst.status in ("active", "degraded")
    ):
        await health.run_test(ctx, db, inst, trigger="manual")
    return await detail(ctx, db, inst)


async def _apply_legal_entities(
    ctx: RequestContext, db: AsyncSession, inst: AppInstallation, manifest: Any, wanted: list[uuid.UUID]
) -> None:
    rows = list(
        (
            await db.execute(
                select(ProviderConnectorConfig).where(ProviderConnectorConfig.installation_id == inst.id)
            )
        ).scalars()
    )
    active = {r.legal_entity_id: r for r in rows if r.status == "active"}
    template = next(iter(active.values()), None) or (rows[0] if rows else None)
    for entity_id, row in active.items():
        if entity_id not in wanted:
            row.status = "disabled"
            await uninstall_extras.kill_jobs(db, inst.id, reason="legal_entity_removed", config_id=row.id)
            await db.execute(
                update(ConnectorSyncState).where(ConnectorSyncState.config_id == row.id).values(enabled=False)
            )
    added = [e for e in wanted if e not in active]
    if added:
        secrets: dict[str, str] = {}
        if template is not None:
            bag = await vault_service.open_bag(
                ctx, db, "provider_config", template.id, purpose="marketplace:legal_entity_added"
            )
            for f in manifest.secret_fields:
                value = bag.get(f.name)
                if value:
                    secrets[f.name] = value
        settings = dict(template.settings or {}) if template is not None else dict(inst.config or {})
        await _create_configs(ctx, db, inst, manifest, settings, added, secrets)
    inst.legal_entity_ids = list(wanted)
    await db.flush()


_STATUS_CHANGES = {
    "suspended": "suspended",
    "active": "resumed",
    "degraded": "degraded",
    "reconsent_required": "reconsent_required",
}


async def set_status(
    ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, status: str, reason: str | None
) -> InstallationOut:
    if status not in _STATUS_CHANGES:
        raise ValidationFailed("Unknown status", details={"fields": {"status": " | ".join(_STATUS_CHANGES)}})
    inst = await _get(db, installation_id)
    if inst.status == "uninstalled":
        raise ConflictError("This installation was removed", code="installation_uninstalled")
    if status == "active" and inst.status == "suspended":
        require_human(actor_from_ctx(ctx))  # resuming external access is a person's decision (★)
        _require_manage(ctx)
        state = await ecosystem_service.is_suspended(inst.tenant_id, app_id=inst.app_id)
        if state.suspended:
            raise ConflictError(
                "External access is suspended for this workspace or app", code="ecosystem_suspended"
            )
    if status == "suspended" and ctx.principal is not None and getattr(ctx.principal, "type", None) == "user":
        _require_manage(ctx)
    previous = inst.status
    inst.status = status
    inst.suspended_reason = (reason or None) if status == "suspended" else None
    if status == "suspended":
        await oauth_service.revoke_for(db, installation_id=inst.id, reason="suspended")
    action = {"suspended": "marketplace.suspended", "active": "marketplace.resumed"}.get(
        status, "installation.status"
    )
    audit.record(
        action,
        "app_installation",
        inst.id,
        session=db,
        status=status,
        previous=previous,
        reason=(reason or "")[:500],
    )
    emit(db, InstallationChanged(
        tenant_id=inst.tenant_id, installation_id=inst.id, kind=inst.kind, app_id=inst.app_id,
        connector_key=inst.connector_key, change=_STATUS_CHANGES[status], status=status, actor_kind=actor_from_ctx(ctx).kind,
    ))  # fmt: skip
    await db.flush()
    oauth_service.clear_cache()
    return _out(inst, await _config_ids(db, inst.id))


async def propagate_erasure(
    ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID
) -> uuid.UUID | None:
    """Uninstall option "Ask X to delete data": run the connector's ``request_erasure`` job while its credentials still
    exist (call before ``uninstall`` in the same transaction). Returns the job id (None when not supported)."""
    require_human(actor_from_ctx(ctx))
    _require_manage(ctx)
    inst = await _get(db, installation_id)
    if inst.status == "uninstalled" or not uninstall_extras.supports_erasure(inst):
        return None
    job = await uninstall_extras.request_erasure(ctx, db, inst)
    return job.id if job is not None else None


async def uninstall(
    ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, *, reason: str
) -> InstallationOut:
    """Revoke tokens, delete vault secrets, disable configs and sync state, queued jobs ``dead``, emit
    ``InstallationChanged(uninstalled)`` (68 deletes the App's subscriptions). Works whatever the adapter / kill switch."""
    require_human(actor_from_ctx(ctx))
    if ctx.principal is not None:
        check(ctx.principal, "integration:manage")
    inst = await _get(db, installation_id)
    if inst.status == "uninstalled":
        return _out(inst, await _config_ids(db, inst.id))
    config_ids = await _config_ids(db, inst.id)
    await oauth_service.revoke_for(db, installation_id=inst.id, reason="uninstalled")
    for cid in config_ids:
        await vault_service.delete_owner(ctx, db, "provider_config", cid)
    await vault_service.delete_owner(ctx, db, "installation", inst.id)
    await db.execute(
        update(ProviderConnectorConfig)
        .where(ProviderConnectorConfig.installation_id == inst.id)
        .values(status="disabled")
    )
    await db.execute(
        update(ConnectorSyncState).where(ConnectorSyncState.installation_id == inst.id).values(enabled=False)
    )
    await uninstall_extras.kill_jobs(db, inst.id)
    inst.status, inst.uninstalled_at, inst.uninstalled_by = "uninstalled", _now(), _user_id(ctx)
    audit.record(
        "marketplace.uninstalled", "app_installation", inst.id, session=db, reason=(reason or "")[:500],
    )  # fmt: skip
    emit(db, InstallationChanged(
        tenant_id=inst.tenant_id, installation_id=inst.id, kind=inst.kind, app_id=inst.app_id,
        connector_key=inst.connector_key, change="uninstalled", status="uninstalled", actor_kind=actor_from_ctx(ctx).kind,
    ))  # fmt: skip
    await db.flush()
    oauth_service.clear_cache()
    return _out(inst, config_ids)


async def test(ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID) -> TestResultOut:
    """ "Test connection" / "Test again" (human). A ``pending_consent`` connector with a current grant becomes ``active``
    when the test passes -- the human click completes the consent (PRD §5.2)."""
    require_human(actor_from_ctx(ctx))
    _require_manage(ctx)
    inst = await _get(db, installation_id)
    if inst.status == "uninstalled":
        raise ConflictError("This installation was removed", code="installation_uninstalled")
    if inst.kind != "connector":
        raise ConflictError("Only connectors have a connection test", code="not_a_connector")
    outcome = await health.run_test(ctx, db, inst, trigger="manual")
    if outcome.ok and inst.status == "pending_consent":
        grant = (
            await db.execute(
                select(AppGrant.id).where(
                    AppGrant.installation_id == inst.id, AppGrant.superseded_at.is_(None)
                )
            )
        ).first()
        if grant is not None:
            inst.status = "active"
            emit(db, InstallationChanged(
                tenant_id=inst.tenant_id, installation_id=inst.id, kind=inst.kind, app_id=None,
                connector_key=inst.connector_key, change="granted", status="active", actor_kind=actor_from_ctx(ctx).kind,
            ))  # fmt: skip
    audit.record(
        "marketplace.tested", "app_installation", inst.id, session=db, ok=outcome.ok, code=outcome.code
    )
    await db.flush()
    return TestResultOut(
        ok=outcome.ok,
        code=outcome.code,
        message=outcome.message,
        status=inst.status,
        health_status=inst.health_status,
    )


async def jobs(
    ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, status: str | None = None
) -> list[Any]:
    from app.models.connectors import ConnectorJob
    from app.services.connectors.jobs import _out as job_out

    if await db.get(AppInstallation, installation_id) is None:
        raise NotFound("Installation not found")
    stmt = select(ConnectorJob).where(ConnectorJob.installation_id == installation_id)
    if status:
        stmt = stmt.where(ConnectorJob.status == status)
    rows = (await db.execute(stmt.order_by(ConnectorJob.created_at.desc()).limit(200))).scalars()
    return [job_out(j) for j in rows]


async def retry_job(
    ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, job_id: uuid.UUID
) -> Any:
    from app.models.connectors import ConnectorJob
    from app.services.connectors import jobs as connector_jobs

    _require_manage(ctx)
    job = await db.get(ConnectorJob, job_id)
    if job is None or job.installation_id != installation_id:
        raise NotFound("Connector job not found")
    return await connector_jobs.retry(ctx, db, job_id)


async def run_sync(ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, job_key: str) -> Any:
    from app.services import integrations_service

    _require_manage(ctx)
    return await integrations_service.run_sync(ctx, db, installation_id, job_key, trigger="manual")


async def preapprove_users(
    ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, data: UserAuthorizationsIn
) -> list[uuid.UUID]:
    """``user_self_authorize=false``: an Integration Administrator pre-approves Users (human; within the grant)."""
    require_human(actor_from_ctx(ctx))
    _require_manage(ctx)
    inst = await _get(db, installation_id)
    if inst.kind != "app" or inst.status not in ("active", "degraded", "reconsent_required"):
        raise ConflictError("Only active apps can have people pre-approved", code="installation_inactive")
    outside = sorted(set(data.scopes) - set(inst.granted_scopes or []))
    if outside:
        raise ValidationFailed(
            "Scopes must be within the installation's grant",
            code="invalid_scope",
            details={"fields": dict.fromkeys(outside, "Not granted")},
        )
    ids: list[uuid.UUID] = []
    for user_id in dict.fromkeys(data.user_ids):
        ids.append(
            await oauth_service.record_user_authorization(
                db, installation_id=inst.id, user_id=user_id, scopes=list(data.scopes), resource=data.resource
            )
        )
    audit.record(
        "marketplace.users_preapproved",
        "app_installation",
        inst.id,
        session=db,
        users=[str(u) for u in data.user_ids],
        scopes=list(data.scopes),
    )
    return ids


async def list_installations(
    ctx: RequestContext,
    db: AsyncSession,
    q: InstallationQuery,
) -> PublicPage[InstallationOut]:
    stmt = select(AppInstallation)
    if q.kind:
        stmt = stmt.where(AppInstallation.kind == q.kind)
    if q.status:
        stmt = stmt.where(AppInstallation.status == q.status)
    elif not q.include_uninstalled:
        stmt = stmt.where(AppInstallation.status != "uninstalled")
    if q.cursor:
        (created,), last_id = decode_cursor(q.cursor)
        stmt = stmt.where(tuple_(AppInstallation.created_at, AppInstallation.id) > tuple_(created, last_id))  # type: ignore[arg-type]
    rows = list(
        (
            await db.execute(stmt.order_by(AppInstallation.created_at, AppInstallation.id).limit(q.limit + 1))
        ).scalars()
    )
    has_more = len(rows) > q.limit
    rows = rows[: q.limit]
    nxt = encode_cursor((rows[-1].created_at,), rows[-1].id) if has_more and rows else None
    ids = [r.id for r in rows]
    last_sync: dict[uuid.UUID, Any] = {}
    pending: dict[uuid.UUID, uuid.UUID] = {}
    if ids:
        last_sync = dict(
            (
                await db.execute(
                    select(ConnectorSyncState.installation_id, func.max(ConnectorSyncState.last_success_at))
                    .where(ConnectorSyncState.installation_id.in_(ids))
                    .group_by(ConnectorSyncState.installation_id)
                )
            )
            .tuples()
            .all()
        )
        from app.models.approvals import ApprovalRequest

        pending = dict(
            (
                await db.execute(
                    select(ApprovalRequest.subject_id, ApprovalRequest.id).where(
                        ApprovalRequest.subject_type == install_approvals.SUBJECT,
                        ApprovalRequest.subject_id.in_(ids),
                        ApprovalRequest.status == "pending",
                    )
                )
            )
            .tuples()
            .all()
        )
    data = [_out(r, last_sync_at=last_sync.get(r.id), approval_request_id=pending.get(r.id)) for r in rows]
    if q.category:
        data = [d for d in data if d.category == q.category]
    return PublicPage[InstallationOut](data=data, next_cursor=nxt, has_more=has_more)


__all__ = [
    "LiveModeGuard",
    "configure",
    "detail",
    "get",
    "install",
    "jobs",
    "list_installations",
    "manifest_digest",
    "preapprove_users",
    "propagate_erasure",
    "register_live_mode_guard",
    "retry_job",
    "run_sync",
    "secret_meta",
    "set_status",
    "test",
    "uninstall",
    "unregister_live_mode_guard",
    "upgrade",
]
