# ruff: file-ignore[line-too-long]
"""The ``connector_triggers`` outbox sink (Epic 66, spec 0.8 / 0.10).

``connector_triggers_sink(ctx, db, rows)`` matches outbox rows (public and internal) against every active connector
Installation's manifest ``sync_jobs`` with ``trigger="event"`` and enqueues one ``connector_jobs`` row per match,
idempotent by ``<connector_key>:<job_key>:<subject_type>:<subject_id>:<seq>`` (a re-delivered batch adds nothing).
Rows written by the Installation itself (``app_installation_id``) are skipped (no echo); jobs without a registered
sync handler are skipped. ``per_legal_entity`` jobs go to the config of ``data.legal_entity_id`` (else the default).

The sink is registered by the coordinator with ``public_events_service.register_sink("connector_triggers",
connector_triggers_sink, include_internal=True)`` (``integrations`` may not import ``public_events_service``).
"""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.integrations import ConnectorManifest, SyncJobSpec, find_manifest
from app.models.connectors import ProviderConnectorConfig
from app.models.oauth import AppInstallation
from app.services.integrations.runner import (
    RUNNABLE_STATUSES,
    enqueue_sync,
    installation_configs,
    sync_handler,
)

__all__ = ["connector_triggers_sink", "event_index"]


def _get(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


async def event_index(
    db: AsyncSession,
) -> dict[str, list[tuple[AppInstallation, ConnectorManifest, SyncJobSpec]]]:
    """``event_type -> [(installation, manifest, sync job)]`` over the tenant's runnable connector Installations."""
    index: dict[str, list[tuple[AppInstallation, ConnectorManifest, SyncJobSpec]]] = {}
    installs = (
        await db.execute(
            select(AppInstallation).where(
                AppInstallation.kind == "connector", AppInstallation.status.in_(RUNNABLE_STATUSES)
            )
        )
    ).scalars()
    for inst in installs:
        manifest = find_manifest(inst.connector_key)
        if manifest is None:
            continue
        for spec in manifest.sync_jobs:
            if spec.trigger != "event" or sync_handler(manifest, spec.key) is None:
                continue
            for event_type in spec.event_types:
                index.setdefault(event_type, []).append((inst, manifest, spec))
    return index


def _pick_config(
    configs: Sequence[ProviderConnectorConfig], spec: SyncJobSpec, data: Mapping[str, Any]
) -> ProviderConnectorConfig | None:
    if not configs:
        return None
    if spec.per_legal_entity:
        raw = data.get("legal_entity_id")
        if raw:
            match = next((c for c in configs if str(c.legal_entity_id) == str(raw)), None)
            if match is not None:
                return match
        return next((c for c in configs if c.legal_entity_id is None), configs[0])
    return configs[0]


async def connector_triggers_sink(ctx: RequestContext, db: AsyncSession, rows: Sequence[Any]) -> int:
    """Outbox sink; returns the number of new or existing jobs addressed (idempotent)."""
    if not rows:
        return 0
    index = await event_index(db)
    if not index:
        return 0
    configs_by_inst: dict[uuid.UUID, list[ProviderConnectorConfig]] = {}
    count = 0
    for row in rows:
        matches = index.get(str(_get(row, "event_type") or ""))
        if not matches:
            continue
        subject_type = str(_get(row, "subject_type") or "")
        subject_id = _get(row, "subject_id")
        seq = _get(row, "seq")
        if not subject_type or subject_id is None:
            continue
        subject_uuid = subject_id if isinstance(subject_id, uuid.UUID) else uuid.UUID(str(subject_id))
        data = _get(row, "data") or {}
        for inst, manifest, spec in matches:
            if _get(row, "app_installation_id") == inst.id:
                continue
            if inst.id not in configs_by_inst:
                configs_by_inst[inst.id] = await installation_configs(db, inst.id)
            config = _pick_config(configs_by_inst[inst.id], spec, data)
            if config is None:
                continue
            await enqueue_sync(
                ctx,
                db,
                inst,
                manifest,
                spec,
                config_id=config.id,
                trigger="event",
                subject=(subject_type, subject_uuid),
                seq=int(seq) if seq is not None else None,
                key=f"{manifest.key}:{spec.key}:{subject_type}:{subject_uuid}:{seq}",
            )
            count += 1
    return count
