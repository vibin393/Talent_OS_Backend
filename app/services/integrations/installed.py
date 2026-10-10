# ruff: noqa: E501, D401
"""Installed-connector operations (Epic 66, spec 0.10): the catalogue view, connection tests, manual sync runs and the
one adapter resolver domain sync handlers use (``adapter_for``)."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.integrations import (
    ConnectorAuthError,
    ConnectorError,
    find_manifest,
    manifest_sha256,
    manifests,
)
from app.models.oauth import AppInstallation
from app.schemas.connectors import ConnectorJobOut
from app.schemas.integrations import ConnectorListingOut, HealthOut
from app.services.connectors import registry as connector_registry
from app.services.integrations.runner import (
    RUNNABLE_STATUSES,
    enqueue_sync,
    installation_configs,
    mark_auth_failed,
    mark_healthy,
)

HEALTH_TIMEOUT_SECONDS = 10


async def available_connectors(ctx: RequestContext, db: AsyncSession) -> list[ConnectorListingOut]:  # noqa: ARG001
    rows = list(
        (
            await db.execute(
                select(AppInstallation).where(
                    AppInstallation.kind == "connector", AppInstallation.status != "uninstalled"
                )
            )
        ).scalars()
    )
    by_key: dict[str, list[uuid.UUID]] = {}
    for r in rows:
        by_key.setdefault(str(r.connector_key), []).append(r.id)
    out: list[ConnectorListingOut] = []
    for m in manifests():
        out.append(
            ConnectorListingOut(
                key=m.key,
                name=m.name,
                summary=m.summary,
                category=m.category,
                capabilities=sorted(m.capabilities),
                auth=m.auth,
                version=m.version,
                manifest_sha256=manifest_sha256(m),
                adapters=sorted(m.adapters),
                data_flows=[
                    {
                        "direction": f.direction,
                        "entity": f.entity,
                        "fields": list(f.fields),
                        "purpose": f.purpose,
                        "destination_country": f.destination_country,
                        "personal_data": f.personal_data,
                    }
                    for f in m.data_flows
                ],
                egress_hosts=list(m.egress_hosts),
                secret_fields=[s.name for s in m.secret_fields],
                config_schema=m.config_model.model_json_schema(),
                installed=m.key in by_key,
                installation_ids=by_key.get(m.key, []),
            )
        )
    return out


async def _installation(db: AsyncSession, installation_id: uuid.UUID) -> AppInstallation:
    inst = await db.get(AppInstallation, installation_id)
    if inst is None or inst.kind != "connector":
        raise NotFound("Installation not found")
    return inst


async def adapter_for(
    ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, *, config_id: uuid.UUID | None = None
) -> Any:
    """The ONLY way domain sync handlers get an adapter (sandbox -> Fake / Manual; Egress Guard; vault secrets)."""
    inst = await _installation(db, installation_id)
    if inst.status not in RUNNABLE_STATUSES:
        raise ConflictError("The installation is not active", code="installation_inactive")
    manifest = find_manifest(str(inst.connector_key))
    if manifest is None:
        raise NotFound("Connector not found")
    configs = await installation_configs(db, inst.id)
    row = (
        next((c for c in configs if c.id == config_id), None)
        if config_id
        else (configs[0] if configs else None)
    )
    if row is None:
        raise ConflictError("The installation has no active configuration", code="config_missing")
    return await connector_registry.build_for(ctx, db, manifest.category, row)


async def test_connection(ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID) -> HealthOut:
    inst = await _installation(db, installation_id)
    adapter = await adapter_for(ctx, db, installation_id)
    error: str | None = None
    try:
        async with asyncio.timeout(HEALTH_TIMEOUT_SECONDS):
            await adapter.handle("health", {})
    except ConnectorAuthError:
        error = "auth_failed"
    except TimeoutError:
        error = "timeout"
    except ConnectorError as exc:
        error = getattr(exc, "code", None) or "error"
    except (
        Exception
    ) as exc:  # adapters without a health op count as reachable unless they raise a connector error
        error = None if isinstance(exc, (NotImplementedError, ValueError)) else "error"
    if error == "auth_failed":
        await mark_auth_failed(db, inst)
    elif error is None:
        await mark_healthy(db, inst)
    else:
        inst.health_status, inst.last_error_code, inst.last_error_at = "failing", error, datetime.now(UTC)
        inst.health_checked_at = datetime.now(UTC)
        await db.flush()
    return HealthOut(
        installation_id=inst.id,
        health_status=inst.health_status,
        error_code=error,
        checked_at=inst.health_checked_at or datetime.now(UTC),
    )


async def run_sync(
    ctx: RequestContext,
    db: AsyncSession,
    installation_id: uuid.UUID,
    job_key: str,
    *,
    trigger: str = "manual",
) -> ConnectorJobOut:
    inst = await _installation(db, installation_id)
    if inst.status not in RUNNABLE_STATUSES:
        raise ConflictError("The installation is not active", code="installation_inactive")
    manifest = find_manifest(str(inst.connector_key))
    spec = manifest.sync_job(job_key) if manifest is not None else None
    if manifest is None or spec is None:
        raise ValidationFailed("Unknown sync job", code="sync_job_unknown", details={"job_key": job_key})
    configs = await installation_configs(db, inst.id)
    if not configs:
        raise ConflictError("The installation has no active configuration", code="config_missing")
    return await enqueue_sync(ctx, db, inst, manifest, spec, config_id=configs[0].id, trigger=trigger)


__all__ = ["adapter_for", "available_connectors", "run_sync", "test_connection"]
