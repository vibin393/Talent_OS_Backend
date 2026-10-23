"""Connector adapter and webhook-handler registries (AD-29).

``get(ctx, db, category, legal_entity_id=None)`` resolves the active ``provider_connector_configs`` row for the
entity, else the tenant default (NULL entity), else the category's ``manual`` adapter.
"""

import dataclasses
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_context
from app.core.errors import ValidationFailed
from app.core.integrations import (
    BridgedFactory,
    ConnectorManifest,
    MappingSecretBag,
    find_manifest,
    is_sandbox_safe,
    make_context,
    on_register,
)
from app.core.registry import Registry
from app.core.tenancy import is_sandbox
from app.models.connectors import CONNECTOR_CATEGORIES, ProviderConnectorConfig
from app.services import vault_service
from app.services.connectors.base import ConnectorConfig, ConnectorPort

AdapterFactory = Callable[[ConnectorConfig | None], ConnectorPort]
# (ctx, db, category, inbound event payload, config) -> None; run by ``connectors.process_inbound``
WebhookHandler = Callable[[RequestContext, AsyncSession, Mapping[str, Any], ConnectorConfig], Awaitable[None]]

_adapters: Registry[tuple[str, str], AdapterFactory] = Registry("connector adapter", allow_replace=True)
_webhook_handlers: Registry[str, WebhookHandler] = Registry("connector webhook handler")


def _check_category(category: str) -> None:
    if category not in CONNECTOR_CATEGORIES:
        msg = f"unknown connector category {category!r}"
        raise ValueError(msg)


def register_adapter(category: str, provider_key: str, factory: AdapterFactory) -> None:
    _check_category(category)
    _adapters.register((category, provider_key), factory)


def adapter_factory(category: str, provider_key: str) -> AdapterFactory | None:
    return _adapters.find((category, provider_key))


def provider_keys(category: str) -> list[str]:
    return sorted(key for (cat, key) in _adapters.view() if cat == category)


def register_webhook_handler(category: str, fn: WebhookHandler) -> None:
    _check_category(category)
    _webhook_handlers.register(category, fn)


def webhook_handler(category: str) -> WebhookHandler | None:
    return _webhook_handlers.find(category)


def config_view(row: ProviderConnectorConfig, secrets: Mapping[str, str] | None = None) -> ConnectorConfig:
    """Build the adapter config from pre-loaded vault ``secrets`` (``credentials`` JSON text and
    ``webhook_secret``). ``load_config`` reads them from the vault (Epic 66, owner ``provider_config``)."""
    secrets = secrets or {}
    raw = secrets.get("credentials")
    credentials: Mapping[str, Any] | None = json.loads(raw) if raw else None
    return ConnectorConfig(
        id=row.id,
        tenant_id=row.tenant_id,
        category=row.category,
        provider_key=row.provider_key,
        legal_entity_id=row.legal_entity_id,
        settings=dict(row.settings or {}),
        credentials=credentials,
        webhook_secret=secrets.get("webhook_secret") or None,
    )


SECRET_NAMES = ("credentials", "webhook_secret")


async def load_config(db: AsyncSession, row: ProviderConnectorConfig) -> ConnectorConfig:
    """``config_view`` with the config's secrets read from the vault (logged reads, purpose ``connector``)."""
    bag = await vault_service.open_bag(get_context(), db, "provider_config", row.id, purpose="connector")
    secrets = {name: value for name in SECRET_NAMES if (value := bag.get(name)) is not None}
    return config_view(row, secrets)


async def write_secrets(
    ctx: RequestContext, db: AsyncSession, config_id: uuid.UUID, changes: Mapping[str, Any]
) -> None:
    """Write-only secret updates of a config: ``credentials`` (dict, stored as sorted JSON) and
    ``webhook_secret`` (str); a key present with an empty value deletes that secret; absent keys stay."""
    for name in SECRET_NAMES:
        if name not in changes:
            continue
        ref = vault_service.SecretRef("provider_config", config_id, name)
        value = changes[name]
        if not value:
            await vault_service.delete(ctx, db, ref)
            continue
        text = json.dumps(value, sort_keys=True) if name == "credentials" else str(value)
        await vault_service.put(ctx, db, ref, text)


async def secret_names(ctx: RequestContext, db: AsyncSession, config_id: uuid.UUID) -> set[str]:
    """Names of the config's stored secrets (no values; no access is logged)."""
    return {m.name for m in await vault_service.meta(ctx, db, "provider_config", config_id)}


def build(category: str, config: ConnectorConfig | None) -> ConnectorPort:
    provider_key = config.provider_key if config is not None else "manual"
    factory = adapter_factory(category, provider_key)
    if factory is None:
        raise ValidationFailed(
            "This connector provider is not available",
            code="connector_provider_unknown",
            details={"category": category, "provider_key": provider_key},
        )
    return factory(config)


async def active_config(
    db: AsyncSession, category: str, legal_entity_id: uuid.UUID | None
) -> ProviderConnectorConfig | None:
    rows = list(
        (
            await db.execute(
                select(ProviderConnectorConfig).where(
                    ProviderConnectorConfig.category == category, ProviderConnectorConfig.status == "active"
                )
            )
        ).scalars()
    )
    if legal_entity_id is not None:
        specific = next((r for r in rows if r.legal_entity_id == legal_entity_id), None)
        if specific is not None:
            return specific
    return next((r for r in rows if r.legal_entity_id is None), None)


async def get(
    ctx: RequestContext,
    db: AsyncSession,
    category: str,
    legal_entity_id: uuid.UUID | None = None,
) -> ConnectorPort:
    _check_category(category)
    row = await active_config(db, category, legal_entity_id)
    return await build_for(ctx, db, category, row)


# ------------------------------------------------------------------------------------- Epic 66 (spec 0.9 / 0.10)
# Manifest bridge: every ``core.integrations`` manifest registers its adapters here as ``BridgedFactory`` objects.
def _bridge_manifest(m: ConnectorManifest) -> None:
    if m.category not in CONNECTOR_CATEGORIES or not m.adapters:
        return  # listing-only manifests (calendar, Epic 72)
    register_adapter(m.category, m.key, BridgedFactory(m))
    for mode in ("fake", "manual"):
        if mode in m.adapters and adapter_factory(m.category, mode) is None:
            register_adapter(m.category, mode, BridgedFactory(m, mode))


on_register(_bridge_manifest)


def _tenant_of(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def _from_factory(category: str, provider_key: str, tenant_id: uuid.UUID, *, sandbox: bool) -> ConnectorPort:
    """Build the ``fake`` / ``manual`` adapter of a category with no configuration (no credentials)."""
    factory = adapter_factory(category, provider_key)
    if isinstance(factory, BridgedFactory):
        ctx = make_context(
            factory.manifest,
            tenant_id=tenant_id,
            installation_id=None,
            config_id=None,
            settings={},
            secrets=MappingSecretBag(),
            mode=factory.mode or provider_key,
            sandbox=sandbox,
        )
        return factory.from_context(ctx)  # type: ignore[no-any-return]
    if factory is None and provider_key != "manual":
        return _from_factory(category, "manual", tenant_id, sandbox=sandbox)
    return build(category, None)


# Epic 69 (additive): (db, installation_id, manifest) -> consented egress hosts, or None for the manifest's own.
EgressPin = Callable[[AsyncSession, uuid.UUID, Any], Awaitable[list[str] | None]]
_egress_pin: EgressPin | None = None


def register_egress_pin(fn: EgressPin | None) -> None:
    global _egress_pin
    _egress_pin = fn


async def build_for(
    ctx: RequestContext, db: AsyncSession, category: str, row: ProviderConnectorConfig | None
) -> ConnectorPort:
    """Resolve the adapter of a configuration row (``None`` = the category's ``manual``).

    * Installation configs (``installation_id`` set, manifest provider) get an ``AdapterContext``: validated
      ``config_model`` (``settings`` minus ``mode``), the vault ``SecretBag`` (owner ``provider_config``) and a
      ``SafeHttpClient`` limited to the manifest's egress hosts.
    * In a Sandbox Tenant only ``fake`` / ``manual`` adapters resolve (FR-339): a real provider falls back to the
      category's ``fake`` (else ``manual``); manifest modes are forced to ``fake``.
    * Legacy configs keep the ``ConnectorConfig`` path."""
    tenant_id = row.tenant_id if row is not None else _tenant_of(db)
    sandbox = await is_sandbox(tenant_id)
    if row is None:
        return _from_factory(category, "manual", tenant_id, sandbox=sandbox)
    manifest = find_manifest(row.provider_key)
    if manifest is not None and manifest.category == category and row.installation_id is not None:
        if _egress_pin is not None:  # Epic 69: keep the consented egress hosts until re-consent (spec A4)
            pinned = await _egress_pin(db, row.installation_id, manifest)
            if pinned is not None:
                manifest = dataclasses.replace(manifest, egress_hosts=tuple(pinned))
        bag = await vault_service.open_bag(ctx, db, "provider_config", row.id, purpose=f"connector:{row.provider_key}")
        try:
            context = make_context(
                manifest,
                tenant_id=tenant_id,
                installation_id=row.installation_id,
                config_id=row.id,
                settings=row.settings,
                secrets=bag,
                sandbox=sandbox,
            )
        except ValidationError as exc:
            raise ValidationFailed(
                "The connector configuration is invalid",
                code="connector_config_invalid",
                details={"provider_key": row.provider_key, "errors": exc.error_count()},
            ) from None
        return BridgedFactory(manifest).from_context(context)  # type: ignore[no-any-return]
    if sandbox and not is_sandbox_safe(row.provider_key, row.settings):
        return _from_factory(category, "fake", tenant_id, sandbox=True)
    return build(category, await load_config(db, row))


def sandbox_safe(provider_key: str, settings: Mapping[str, Any] | None = None) -> bool:
    """True when a configuration resolves to a ``fake`` / ``manual`` adapter (allowed in a Sandbox Tenant)."""
    return is_sandbox_safe(provider_key, settings)


# Op runners (``connector_jobs.op`` prefix -> runner, e.g. ``sync:`` registered by ``integrations``) and job listeners
# (called for jobs with ``installation_id`` after each classified outcome). Registered from hooks; P2 code never
# imports the Phase 5 packages.
JobRunner = Callable[[RequestContext, AsyncSession, Any], Awaitable[Mapping[str, Any]]]
# (ctx, db, job, outcome, exc) with outcome in succeeded | retry | dead | auth_failed | rate_limited | permanent | manual
JobListener = Callable[[RequestContext, AsyncSession, Any, str, BaseException | None], Awaitable[None]]
_job_runners: Registry[str, JobRunner] = Registry("connector job runner", allow_replace=True)
_job_listeners: list[JobListener] = []


def register_job_runner(op_prefix: str, fn: JobRunner) -> None:
    _job_runners.register(op_prefix, fn)


def job_runner(op: str) -> JobRunner | None:
    for prefix, fn in _job_runners.view().items():
        if op.startswith(prefix):
            return fn
    return None


def register_job_listener(fn: JobListener) -> None:
    if fn not in _job_listeners:
        _job_listeners.append(fn)


def job_listeners() -> list[JobListener]:
    return list(_job_listeners)


async def webhook_secret(ctx: RequestContext, db: AsyncSession, row: ProviderConnectorConfig) -> str | None:
    """The webhook verification secret of an Installation config (the manifest's ``webhook_secret`` field, from the
    vault; logged read) -- legacy configs keep ``webhook_secret``."""
    manifest = find_manifest(row.provider_key)
    name = manifest.webhook_secret_name() if manifest is not None else "webhook_secret"
    bag = await vault_service.open_bag(ctx, db, "provider_config", row.id, purpose="connector_webhook")
    return bag.get(name)
