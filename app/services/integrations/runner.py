# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Connector sync runner (Epic 66, spec 0.10, AD-81).

Every sync / push of an installed connector runs as a ``connector_jobs`` row (``installation_id`` set, op
``sync:<job_key>``) on queue ``ecosystem``; ``connectors.jobs.execute`` (retry / back-off / dead, kill-switch gate,
error classification) calls ``run_sync_job`` through the registered op runner. Domain sync handlers (Epics 70-72)
register with ``register_sync_handler(connector_key_or_category, job_key, fn)`` and get the adapter in ``SyncRun``.

* ``connector_sync_state`` keeps the cursor per (installation, per-entity config, job); ``lock_until`` prevents
  overlapping scheduled runs (``fire_due``, beat ``integrations.fire_schedules`` every 60 s).
* Job outcomes (``on_job_outcome``): ``auth_failed`` -> Installation ``degraded`` (``last_error_code='auth_failed'``,
  ``ConnectorHealthChanged``); terminal failures of a sync -> ``ConnectorSyncFailed``; a success restores health.
"""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import emit
from app.core.integrations import ConnectorManifest, ConnectorPermanentError, SyncJobSpec, find_manifest
from app.events.connectors import ConnectorJobQueued
from app.events.ecosystem import ConnectorHealthChanged, ConnectorSyncFailed
from app.models.connectors import ConnectorJob, ProviderConnectorConfig
from app.models.integrations import ConnectorSyncState
from app.models.oauth import AppInstallation
from app.schemas.connectors import ConnectorJobOut
from app.services.connectors import registry as connector_registry

__all__ = [
    "MAX_ERRORS",
    "RUNNABLE_STATUSES",
    "SYNC_OP_PREFIX",
    "SyncError",
    "SyncHandler",
    "SyncResult",
    "SyncRun",
    "enqueue_sync",
    "fire_due",
    "on_job_outcome",
    "register_sync_handler",
    "run_sync_job",
    "sync_handler",
]

SYNC_OP_PREFIX = "sync:"
RUNNABLE_STATUSES = ("active", "degraded", "reconsent_required")  # Epic 69: the consented version keeps running
MAX_ERRORS = 500
LOCK_SECONDS = 900  # a scheduled run holds its state row this long (cleared when the run ends)


@dataclasses.dataclass(frozen=True, slots=True)
class SyncError:
    entity_type: str
    local_id: uuid.UUID | None
    external_id: str | None
    code: str
    message: str


@dataclasses.dataclass(frozen=True, slots=True)
class SyncResult:
    processed: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    next_cursor: Mapping[str, Any] | None = None
    errors: tuple[SyncError, ...] = ()  # at most 500 are kept


@dataclasses.dataclass(frozen=True)
class SyncRun:
    installation_id: uuid.UUID
    config_id: uuid.UUID | None
    job_key: str
    trigger: str
    cursor: Mapping[str, Any]
    subject: tuple[str, uuid.UUID] | None
    adapter: Any


SyncHandler = Callable[[RequestContext, AsyncSession, SyncRun], Awaitable[SyncResult]]

_handlers: dict[tuple[str, str], SyncHandler] = {}


def register_sync_handler(connector_key_or_category: str, job_key: str, fn: SyncHandler) -> None:
    """Register the domain handler of a manifest sync job, by connector key (wins) or by category."""
    _handlers[connector_key_or_category, job_key] = fn


def unregister_sync_handler(connector_key_or_category: str, job_key: str) -> None:
    """Tests only."""
    _handlers.pop((connector_key_or_category, job_key), None)


def sync_handler(manifest: ConnectorManifest, job_key: str) -> SyncHandler | None:
    return _handlers.get((manifest.key, job_key)) or _handlers.get((manifest.category, job_key))


def _now() -> datetime:
    return datetime.now(UTC)


def _tenant_of(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def _job_out(job: ConnectorJob) -> ConnectorJobOut:
    out = ConnectorJobOut.model_validate(job)
    created = job.created_at or _now()
    return out.model_copy(update={"age_seconds": max(int((_now() - created).total_seconds()), 0)})


async def installation_configs(db: AsyncSession, installation_id: uuid.UUID) -> list[ProviderConnectorConfig]:
    """Active configs of an Installation (tenant default first, then by creation)."""
    rows = (
        await db.execute(
            select(ProviderConnectorConfig)
            .where(
                ProviderConnectorConfig.installation_id == installation_id,
                ProviderConnectorConfig.status == "active",
            )
            .order_by(
                ProviderConnectorConfig.legal_entity_id.is_not(None), ProviderConnectorConfig.created_at
            )
        )
    ).scalars()
    return list(rows)


async def enqueue_sync(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    installation: AppInstallation,
    manifest: ConnectorManifest,
    spec: SyncJobSpec,
    *,
    config_id: uuid.UUID | None,
    trigger: str,
    subject: tuple[str, uuid.UUID] | None = None,
    seq: int | None = None,
    key: str | None = None,
) -> ConnectorJobOut:
    """Insert the ``sync:<job_key>`` job (idempotent by ``key``; default a fresh key per call) and queue it after
    commit on ``ecosystem``."""
    tenant_id = _tenant_of(db)
    subject_kind, subject_id = subject if subject is not None else ("installation", installation.id)
    idem = (
        key or f"{manifest.key}:{spec.key}:{trigger}:{config_id or installation.id}:{uuid.uuid4().hex[:16]}"
    )
    payload: dict[str, Any] = {
        "installation_id": str(installation.id),
        "connector_key": manifest.key,
        "job_key": spec.key,
        "trigger": trigger,
    }
    if subject is not None:
        payload["subject_type"], payload["subject_id"] = subject_kind, str(subject_id)
    if seq is not None:
        payload["seq"] = seq
    inserted = (
        await db.execute(
            pg_insert(ConnectorJob)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                category=manifest.category,
                op=f"{SYNC_OP_PREFIX}{spec.key}",
                subject_kind=subject_kind[:40],
                subject_id=subject_id,
                version=int(seq or 1) if (seq or 1) < 2**31 else 1,
                idempotency_key=idem[:200],
                config_id=config_id,
                installation_id=installation.id,
                status="queued",
                attempts=0,
                max_attempts=5,
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
            .returning(ConnectorJob.id)
        )
    ).scalar_one_or_none()
    job = (
        await db.execute(select(ConnectorJob).where(ConnectorJob.idempotency_key == idem[:200]))
    ).scalar_one()
    if inserted is not None:
        emit(db, ConnectorJobQueued(tenant_id=tenant_id, job_id=job.id, category=job.category, op=job.op))
    return _job_out(job)


async def _state(
    db: AsyncSession,
    installation_id: uuid.UUID,
    config_id: uuid.UUID | None,
    job_key: str,
    *,
    lock: bool = False,
) -> ConnectorSyncState:
    stmt = select(ConnectorSyncState).where(
        ConnectorSyncState.installation_id == installation_id,
        ConnectorSyncState.job_key == job_key,
        ConnectorSyncState.config_id.is_(None)
        if config_id is None
        else ConnectorSyncState.config_id == config_id,
    )
    row = (await db.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    if row is None:
        await db.execute(
            pg_insert(ConnectorSyncState)
            .values(
                id=uuid.uuid4(),
                tenant_id=_tenant_of(db),
                installation_id=installation_id,
                config_id=config_id,
                job_key=job_key,
            )
            .on_conflict_do_nothing(constraint="uq_connector_sync_state_job")
        )
        row = (await db.execute(stmt.with_for_update() if lock else stmt)).scalar_one()
    return row


def _state_config(spec: SyncJobSpec, config_id: uuid.UUID | None) -> uuid.UUID | None:
    return config_id if spec.per_legal_entity else None


def _resolve(
    installation: AppInstallation | None, job_key: str
) -> tuple[ConnectorManifest, SyncJobSpec, SyncHandler]:
    if (
        installation is None
        or installation.kind != "connector"
        or installation.status not in RUNNABLE_STATUSES
    ):
        raise ConnectorPermanentError("installation_inactive", "The Installation is not active")
    manifest = find_manifest(installation.connector_key)
    if manifest is None:
        raise ConnectorPermanentError("connector_unknown", "The connector is not available")
    spec = manifest.sync_job(job_key)
    if spec is None:
        raise ConnectorPermanentError("sync_job_unknown", "The connector has no such sync job")
    handler = sync_handler(manifest, job_key)
    if handler is None:
        raise ConnectorPermanentError("sync_handler_missing", "No handler is registered for this sync job")
    return manifest, spec, handler


async def run_sync_job(ctx: RequestContext, db: AsyncSession, job: ConnectorJob) -> Mapping[str, Any]:
    """The ``sync:`` op runner (called by ``connectors.jobs.execute``). Handler writes run in a SAVEPOINT: a raised
    error rolls them back and the job layer classifies it."""
    installation = await db.get(AppInstallation, job.installation_id) if job.installation_id else None
    job_key = job.op[len(SYNC_OP_PREFIX) :]
    manifest, spec, handler = _resolve(installation, job_key)
    assert installation is not None
    config_row = await db.get(ProviderConnectorConfig, job.config_id) if job.config_id else None
    if config_row is None:
        configs = await installation_configs(db, installation.id)
        config_row = configs[0] if configs else None
    if config_row is None or config_row.installation_id != installation.id:
        raise ConnectorPermanentError("config_missing", "The Installation has no active configuration")
    adapter = await connector_registry.build_for(ctx, db, manifest.category, config_row)
    state = await _state(db, installation.id, _state_config(spec, config_row.id), job_key, lock=True)
    payload = dict(job.payload or {})
    subject = None
    if payload.get("subject_type") and payload.get("subject_id"):
        subject = (str(payload["subject_type"]), uuid.UUID(str(payload["subject_id"])))
    run = SyncRun(
        installation_id=installation.id,
        config_id=config_row.id,
        job_key=job_key,
        trigger=str(payload.get("trigger") or "manual"),
        cursor=dict(state.cursor or {}),
        subject=subject,
        adapter=adapter,
    )
    async with db.begin_nested():
        result = await handler(ctx, db, run)
    now = _now()
    if result.next_cursor is not None:
        state.cursor = dict(result.next_cursor)
    state.last_run_at = now
    state.last_success_at = now
    state.consecutive_failures = 0
    state.last_error_code = None
    state.lock_until = None
    await db.flush()
    errors = [
        dataclasses.asdict(e) | {"local_id": str(e.local_id) if e.local_id else None}
        for e in result.errors[:MAX_ERRORS]
    ]
    return {
        "processed": result.processed,
        "created": result.created,
        "updated": result.updated,
        "skipped": result.skipped,
        "failed": result.failed,
        "errors": errors,
        "errors_truncated": len(result.errors) > MAX_ERRORS,
    }


def _code(exc: BaseException | None, outcome: str) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code[:60]
    return "error" if outcome != "dead" else "retries_exhausted"


async def _set_health(
    db: AsyncSession, inst: AppInstallation, health: str, error_code: str | None, *, degrade: bool = False
) -> None:
    changed = inst.health_status != health
    now = _now()
    inst.health_status = health
    inst.health_checked_at = now
    if error_code is not None:
        inst.last_error_code = error_code
        inst.last_error_at = now
    if degrade and inst.status == "active":
        inst.status = "degraded"
        changed = True
    if not degrade and health == "ok":
        if inst.status == "degraded" and inst.last_error_code == "auth_failed":
            inst.status = "active"
            changed = True
        inst.last_error_code = None
    await db.flush()
    if changed:
        emit(
            db,
            ConnectorHealthChanged(
                tenant_id=inst.tenant_id, installation_id=inst.id, health_status=health, error_code=error_code
            ),
        )


async def mark_auth_failed(db: AsyncSession, inst: AppInstallation) -> None:
    """Auth failure: Installation ``degraded``, ``last_error_code='auth_failed'``, ``ConnectorHealthChanged``."""
    await _set_health(db, inst, "degraded", "auth_failed", degrade=True)


async def mark_healthy(db: AsyncSession, inst: AppInstallation) -> None:
    if inst.health_status != "ok" or inst.status == "degraded":
        await _set_health(db, inst, "ok", None)


async def on_job_outcome(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    job: ConnectorJob,
    outcome: str,
    exc: BaseException | None,
) -> None:
    """Job listener for Installation jobs (registered with ``connectors.registry.register_job_listener``)."""
    inst = await db.get(AppInstallation, job.installation_id) if job.installation_id else None
    if inst is None:
        return
    if outcome == "auth_failed":
        await mark_auth_failed(db, inst)
    elif outcome in ("succeeded", "manual"):
        await mark_healthy(db, inst)
    if not job.op.startswith(SYNC_OP_PREFIX) or outcome in ("succeeded", "manual", "rate_limited"):
        return
    manifest = find_manifest(inst.connector_key)
    job_key = job.op[len(SYNC_OP_PREFIX) :]
    spec = manifest.sync_job(job_key) if manifest is not None else None
    code = _code(exc, outcome)
    state = await _state(db, inst.id, _state_config(spec, job.config_id) if spec else None, job_key)
    state.last_run_at = _now()
    state.last_error_code = code
    if outcome == "retry":
        await db.flush()
        return
    state.consecutive_failures += 1
    state.lock_until = None
    await db.flush()
    emit(
        db,
        ConnectorSyncFailed(
            tenant_id=inst.tenant_id,
            installation_id=inst.id,
            config_id=job.config_id,
            job_key=job_key,
            error_code=code,
            consecutive_failures=state.consecutive_failures,
        ),
    )


async def fire_due(ctx: RequestContext, db: AsyncSession, *, now: datetime | None = None) -> int:
    """Enqueue every due scheduled sync of the tenant (beat ``integrations.fire_schedules``). Creates missing state
    rows (due at once), skips rows whose ``lock_until`` is in the future, then locks each fired row for
    ``LOCK_SECONDS`` and moves ``next_run_at`` by ``every_seconds``. Returns the number of jobs queued."""
    now = now or _now()
    installations = list(
        (
            await db.execute(
                select(AppInstallation).where(
                    AppInstallation.kind == "connector", AppInstallation.status.in_(RUNNABLE_STATUSES)
                )
            )
        ).scalars()
    )
    plans: dict[uuid.UUID, tuple[AppInstallation, ConnectorManifest, list[ProviderConnectorConfig]]] = {}
    for inst in installations:
        manifest = find_manifest(inst.connector_key)
        if manifest is None:
            continue
        specs = [s for s in manifest.sync_jobs if s.trigger == "schedule" and sync_handler(manifest, s.key)]
        if not specs:
            continue
        configs = await installation_configs(db, inst.id)
        if not configs:
            continue
        plans[inst.id] = (inst, manifest, configs)
        for spec in specs:
            for cid in [c.id for c in configs] if spec.per_legal_entity else [None]:  # type: ignore[list-item]
                await db.execute(
                    pg_insert(ConnectorSyncState)
                    .values(
                        id=uuid.uuid4(),
                        tenant_id=inst.tenant_id,
                        installation_id=inst.id,
                        config_id=cid,
                        job_key=spec.key,
                        next_run_at=now,
                    )
                    .on_conflict_do_nothing(constraint="uq_connector_sync_state_job")
                )
    if not plans:
        return 0
    due = list(
        (
            await db.execute(
                select(ConnectorSyncState)
                .where(
                    ConnectorSyncState.installation_id.in_(list(plans)),
                    ConnectorSyncState.enabled.is_(True),
                    ConnectorSyncState.next_run_at.is_not(None),
                    ConnectorSyncState.next_run_at <= now,
                    or_(ConnectorSyncState.lock_until.is_(None), ConnectorSyncState.lock_until < now),
                )
                .order_by(ConnectorSyncState.next_run_at)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    fired = 0
    for st in due:
        inst, manifest, configs = plans[st.installation_id]
        spec = manifest.sync_job(st.job_key)  # type: ignore[assignment]
        if spec is None or spec.trigger != "schedule" or sync_handler(manifest, spec.key) is None:
            continue
        config_id = st.config_id or configs[0].id
        if st.config_id is not None and all(c.id != st.config_id for c in configs):
            continue  # the entity's config was removed
        st.lock_until = now + timedelta(seconds=LOCK_SECONDS)
        st.next_run_at = now + timedelta(seconds=int(spec.every_seconds or 900))
        await enqueue_sync(
            ctx,
            db,
            inst,
            manifest,
            spec,
            config_id=config_id,
            trigger="schedule",
            key=f"{manifest.key}:{spec.key}:schedule:{st.id}:{int(now.timestamp())}",
        )
        fired += 1
    await db.flush()
    if fired:
        logger.debug("Fired {} scheduled connector syncs", fired)
    return fired
