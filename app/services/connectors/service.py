"""Provider connector configurations (AD-29; ``connector:configure``).

Credentials (JSON) and webhook secrets live in the Secrets Vault (Epic 66; owner ``provider_config``, written
through ``registry.write_secrets``), write-only and never audited or returned (``has_*`` flags come from
vault meta). Creating a config also writes the platform lookup ``connector_hook_lookups(config_id -> tenant)``
used by the public webhook endpoint.
"""

import dataclasses
import ipaddress
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.database import platform_session
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.integrations import find_manifest
from app.core.settings import get_settings
from app.core.signing import verify_hmac
from app.core.tenancy import is_sandbox
from app.models.connectors import ConnectorHookLookup, ProviderConnectorConfig
from app.schemas.connectors import ConnectorConfigIn, ConnectorConfigOut, ConnectorConfigUpdate
from app.services.connectors import jobs, registry


async def _out(ctx: RequestContext, db: AsyncSession, row: ProviderConnectorConfig) -> ConnectorConfigOut:
    names = await registry.secret_names(ctx, db, row.id)
    return ConnectorConfigOut(
        id=row.id,
        category=row.category,
        provider_key=row.provider_key,
        legal_entity_id=row.legal_entity_id,
        display_name=row.display_name,
        settings=dict(row.settings or {}),
        status=row.status,
        has_credentials="credentials" in names,
        has_webhook_secret="webhook_secret" in names,
        webhook_path=f"/api/v1/hooks/{row.category}/{row.id}",
        created_at=row.created_at,
    )


async def _write_lookup(config_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    async with platform_session() as pdb:
        await pdb.execute(
            pg_insert(ConnectorHookLookup)
            .values(config_id=config_id, tenant_id=tenant_id)
            .on_conflict_do_nothing(index_elements=["config_id"])
        )


async def _refuse_real_in_sandbox(db: AsyncSession, category: str, provider_key: str, settings: Any) -> None:
    """Epic 66 (FR-339): a Sandbox Tenant may configure only Fake / Manual adapters."""
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    if await is_sandbox(tenant_id) and not registry.sandbox_safe(provider_key, settings):
        raise ValidationFailed(
            "Sandbox tenants can use only Fake or Manual connectors",
            code="sandbox_real_adapter",
            details={"category": category, "provider_key": provider_key},
        )


async def list_configs(ctx: RequestContext, db: AsyncSession) -> list[ConnectorConfigOut]:
    rows = (
        await db.execute(
            select(ProviderConnectorConfig).order_by(ProviderConnectorConfig.category, ProviderConnectorConfig.created_at)
        )
    ).scalars()
    return [await _out(ctx, db, r) for r in rows]


async def create_config(ctx: RequestContext, db: AsyncSession, data: ConnectorConfigIn) -> ConnectorConfigOut:
    if registry.adapter_factory(data.category, data.provider_key) is None:
        raise ValidationFailed(
            "This connector provider is not available",
            code="connector_provider_unknown",
            details={"category": data.category, "provider_key": data.provider_key},
        )
    await _refuse_real_in_sandbox(db, data.category, data.provider_key, data.settings)
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    row = ProviderConnectorConfig(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        category=data.category,
        provider_key=data.provider_key,
        legal_entity_id=data.legal_entity_id,
        display_name=data.display_name,
        settings=data.settings,
        status="active",
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "An active configuration already exists for this category and legal entity", code="connector_config_exists"
        ) from exc
    await registry.write_secrets(
        ctx, db, row.id, {"credentials": data.credentials, "webhook_secret": data.webhook_secret}
    )
    await _write_lookup(row.id, tenant_id)
    await db.refresh(row)
    return await _out(ctx, db, row)


async def update_config(
    ctx: RequestContext,
    db: AsyncSession,
    config_id: uuid.UUID,
    data: ConnectorConfigUpdate,
) -> ConnectorConfigOut:
    row = (
        await db.execute(select(ProviderConnectorConfig).where(ProviderConnectorConfig.id == config_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Connector configuration not found")
    changes = data.model_dump(exclude_unset=True)
    new_settings = changes.get("settings") if changes.get("settings") is not None else row.settings
    if (changes.get("settings") is not None or changes.get("status") == "active") and (
        changes.get("status") or row.status
    ) == "active":
        await _refuse_real_in_sandbox(db, row.category, row.provider_key, new_settings)
    if changes.get("display_name") is not None:
        row.display_name = changes["display_name"]
    await registry.write_secrets(ctx, db, row.id, changes)
    if changes.get("settings") is not None:
        row.settings = changes["settings"]
    if changes.get("status") is not None:
        row.status = changes["status"]
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "An active configuration already exists for this category and legal entity", code="connector_config_exists"
        ) from exc
    await db.refresh(row)
    return await _out(ctx, db, row)


async def tenant_for_hook(config_id: uuid.UUID) -> uuid.UUID | None:
    async with platform_session() as pdb:
        return (
            await pdb.execute(select(ConnectorHookLookup.tenant_id).where(ConnectorHookLookup.config_id == config_id))
        ).scalar_one_or_none()


async def drop_lookup(config_id: uuid.UUID) -> None:
    async with platform_session() as pdb:
        await pdb.execute(delete(ConnectorHookLookup).where(ConnectorHookLookup.config_id == config_id))


@dataclasses.dataclass(frozen=True, slots=True)
class InboundAck:
    """Result of ``ingest_webhook``: ``accepted`` (queued), ``held`` (Ecosystem Kill Switch on) or ``duplicate``
    (same provider event id already stored: acknowledged 200 and dropped)."""

    event_id: uuid.UUID
    status: str


def _json_pointer(doc: Any, pointer: str | None) -> str | None:
    if not pointer:
        return None
    cur = doc
    for raw in pointer.lstrip("/").split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    if cur is None or isinstance(cur, (dict, list)):
        return None
    return str(cur)[:200]


def _ip_allowed(client_ip: str | None, cidrs: tuple[str, ...]) -> bool:
    if not cidrs:
        return True
    if not client_ip:
        return False
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(c, strict=False) for c in cidrs)


async def _verify(
    ctx: RequestContext,
    db: AsyncSession,
    row: ProviderConnectorConfig,
    adapter: Any,
    headers: Mapping[str, str],
    body: bytes,
    client_ip: str | None,
) -> bool:
    """Installation configs verify through the manifest ``WebhookSpec`` (``core.signing``: constant time, timestamp
    tolerance; IP allow-list); ``custom`` schemes and legacy configs use the adapter."""
    manifest = find_manifest(row.provider_key) if row.installation_id is not None else None
    spec = manifest.webhook if manifest is not None else None
    if manifest is None or spec is None:
        return bool(adapter.verify_webhook(headers, body))
    if not _ip_allowed(client_ip, spec.ip_allowlist):
        return False
    if spec.scheme == "custom":
        return bool(adapter.verify_webhook(headers, body))
    secret = await registry.webhook_secret(ctx, db, row)
    if not secret:
        return False
    url = f"{get_settings().p5_public_webhook_base_url()}/api/v1/hooks/{row.category}/{row.id}"
    return verify_hmac(
        spec.scheme,
        secret.encode(),
        headers,
        body,
        url,
        signature_header=spec.signature_header,
        timestamp_header=spec.timestamp_header,
        tolerance=spec.tolerance_seconds,
    )


async def _find_event(db: AsyncSession, config_id: uuid.UUID, external_id: str) -> uuid.UUID | None:
    from app.models.connectors import ConnectorInboundEvent

    return (
        await db.execute(
            select(ConnectorInboundEvent.id).where(
                ConnectorInboundEvent.config_id == config_id,
                ConnectorInboundEvent.external_event_id == external_id,
            )
        )
    ).scalar_one_or_none()


async def ingest_webhook(
    category: str, config_id: uuid.UUID, headers: dict[str, str], body: bytes, *, client_ip: str | None = None
) -> InboundAck:
    """Public webhook (``POST /api/v1/hooks/{category}/{config_id}``): resolve the tenant through the platform
    lookup, verify the signature (401 otherwise), dedup by the provider event id (``WebhookSpec.event_id_path``),
    store the payload -- ``held`` while the Ecosystem Kill Switch is on (Epic 66, FR-340) -- and queue
    ``connectors.process_inbound`` after commit. Unknown config / category -> 404."""
    from app.core.database import tenant_session
    from app.core.errors import Unauthorized
    from app.models.connectors import ConnectorInboundEvent
    from app.tasks import connectors as connector_tasks
    from app.tasks.runtime import enqueue

    tenant_id = await tenant_for_hook(config_id)
    if tenant_id is None:
        raise NotFound("Not found")
    ctx = RequestContext(tenant_id=tenant_id)
    async with tenant_session(tenant_id) as db:
        row = (
            await db.execute(
                select(ProviderConnectorConfig).where(
                    ProviderConnectorConfig.id == config_id,
                    ProviderConnectorConfig.category == category,
                    ProviderConnectorConfig.status == "active",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound("Not found")
        adapter = await registry.build_for(ctx, db, category, row)
        if not await _verify(ctx, db, row, adapter, headers, body, client_ip):
            raise Unauthorized("Invalid webhook signature", code="webhook_signature_invalid")
        payload = dict(adapter.parse_webhook(headers, body))
        manifest = find_manifest(row.provider_key)
        spec = manifest.webhook if manifest is not None else None
        external_id = _json_pointer(payload, spec.event_id_path) if spec is not None else None
        if external_id is not None:
            existing = await _find_event(db, row.id, external_id)
            if existing is not None:
                return InboundAck(existing, "duplicate")
        held = await jobs.ecosystem_suspended(tenant_id)
        event = ConnectorInboundEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            config_id=row.id,
            category=category,
            payload=payload,
            status="held" if held else "received",
            external_event_id=external_id,
        )
        try:
            async with db.begin_nested():
                db.add(event)
                await db.flush()
        except IntegrityError:
            dup = await _find_event(db, row.id, external_id or "")
            if dup is None:
                raise
            return InboundAck(dup, "duplicate")
        event_id = event.id
    if held:
        return InboundAck(event_id, "held")
    enqueue(connector_tasks.process_inbound, str(tenant_id), str(event_id))
    return InboundAck(event_id, "accepted")


async def receive_webhook(category: str, config_id: uuid.UUID, headers: dict[str, str], body: bytes) -> uuid.UUID:
    """Phase 2 entry point (kept): ``ingest_webhook`` without the outcome."""
    return (await ingest_webhook(category, config_id, headers, body)).event_id


async def release_held(tenant_id: uuid.UUID) -> list[uuid.UUID]:
    """Epic 66 (FR-340): once the Ecosystem Kill Switch is lifted, process the tenant's ``held`` inbound events in
    arrival order (one transaction each). Returns the processed ids; nothing while still suspended."""
    from app.core.database import tenant_session
    from app.models.connectors import ConnectorInboundEvent

    if await jobs.ecosystem_suspended(tenant_id):
        return []
    async with tenant_session(tenant_id) as db:
        rows = list(
            (
                await db.execute(
                    select(ConnectorInboundEvent)
                    .where(ConnectorInboundEvent.status == "held")
                    .order_by(ConnectorInboundEvent.received_at, ConnectorInboundEvent.id)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        for ev in rows:
            ev.status = "received"
        await db.flush()
        ids = [ev.id for ev in rows]
    ctx = RequestContext(tenant_id=tenant_id)
    for event_id in ids:
        async with tenant_session(tenant_id) as db:
            await jobs.process_inbound(ctx, db, event_id)
    return ids
