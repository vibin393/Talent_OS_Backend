"""Outbound connector jobs (AD-29): idempotent by ``<category>:<op>:<subject_kind>:<subject_id>:<version>``.

``enqueue`` inserts the job (``ON CONFLICT DO NOTHING``; an existing job is returned unchanged) and emits
``ConnectorJobQueued``; the after-commit handler queues ``connectors.run_job`` on the ``connectors`` queue.
``execute`` (the task body) records every attempt; failures retry with bounded exponential back-off
(30 s x 2^n, at most ``max_attempts`` = 5), then the job is ``dead`` and ``ConnectorJobFailed`` is emitted.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.events import emit, subscribe
from app.core.integrations.errors import ConnectorAuthError, ConnectorPermanentError, ConnectorRateLimited
from app.events.connectors import ConnectorJobFailed, ConnectorJobQueued
from app.models.connectors import ConnectorInboundEvent, ConnectorJob, ProviderConnectorConfig
from app.schemas.connectors import ConnectorJobOut
from app.services.connectors import registry
from app.services.connectors.base import ManualActionRequired

BACKOFF_BASE_SECONDS = 30
SUSPENDED_RETRY_SECONDS = 300  # Epic 66: kill switch on -> outbound jobs wait 5 min (attempt not counted)
DEFERRED = "deferred"  # Epic 66: ``execute`` result of an uncounted reschedule
MAX_ERROR = 2000


def _now() -> datetime:
    return datetime.now(UTC)


def idempotency_key(category: str, op: str, subject_kind: str, subject_id: uuid.UUID, version: int) -> str:
    return f"{category}:{op}:{subject_kind}:{subject_id}:{version}"


def _out(job: ConnectorJob) -> ConnectorJobOut:
    out = ConnectorJobOut.model_validate(job)
    created = job.created_at or _now()
    return out.model_copy(update={"age_seconds": max(int((_now() - created).total_seconds()), 0)})


async def enqueue(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    category: str,
    op: str,
    subject_kind: str,
    subject_id: uuid.UUID,
    version: int,
    payload: dict[str, Any],
) -> ConnectorJobOut:
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    key = idempotency_key(category, op, subject_kind, subject_id, version)
    config = await registry.active_config(db, category, payload.get("legal_entity_id") and uuid.UUID(str(payload["legal_entity_id"])))
    inserted = (
        await db.execute(
            pg_insert(ConnectorJob)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                category=category,
                op=op,
                subject_kind=subject_kind,
                subject_id=subject_id,
                version=version,
                idempotency_key=key,
                config_id=config.id if config is not None else None,
                status="queued",
                attempts=0,
                max_attempts=5,
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
            .returning(ConnectorJob.id)
        )
    ).scalar_one_or_none()
    job = (await db.execute(select(ConnectorJob).where(ConnectorJob.idempotency_key == key))).scalar_one()
    if inserted is not None:
        emit(db, ConnectorJobQueued(tenant_id=tenant_id, job_id=job.id, category=category, op=op))
    return _out(job)


async def pending(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    category: str | None = None,
    status: str | None = None,
) -> list[ConnectorJobOut]:
    stmt = select(ConnectorJob)
    if category:
        stmt = stmt.where(ConnectorJob.category == category)
    stmt = stmt.where(ConnectorJob.status == status) if status else stmt.where(ConnectorJob.status.in_(("queued", "running", "failed")))
    rows = (await db.execute(stmt.order_by(ConnectorJob.created_at).limit(500))).scalars()
    return [_out(j) for j in rows]


async def retry(ctx: RequestContext, db: AsyncSession, job_id: uuid.UUID) -> ConnectorJobOut:  # ruff: ignore[unused-function-argument]
    job = (await db.execute(select(ConnectorJob).where(ConnectorJob.id == job_id).with_for_update())).scalar_one_or_none()
    if job is None:
        raise NotFound("Connector job not found")
    if job.status not in ("failed", "dead"):
        raise ConflictError("Only failed jobs can be retried", code="job_state", details={"status": job.status})
    job.status = "queued"
    job.attempts = 0
    job.next_attempt_at = None
    job.last_error = None
    await db.flush()
    emit(db, ConnectorJobQueued(tenant_id=job.tenant_id, job_id=job.id, category=job.category, op=job.op))
    return _out(job)


async def ecosystem_suspended(tenant_id: uuid.UUID) -> bool:
    """Epic 66 (FR-340): True while the Ecosystem Kill Switch (tenant, platform-tenant or global) is on."""
    from app.services import ecosystem_service

    return bool((await ecosystem_service.is_suspended(tenant_id)).suspended)


async def _notify(
    ctx: RequestContext, db: AsyncSession, job: ConnectorJob, outcome: str, exc: BaseException | None
) -> None:
    if job.installation_id is None:
        return
    for listener in registry.job_listeners():
        try:
            async with db.begin_nested():
                await listener(ctx, db, job, outcome, exc)
        except Exception:
            logger.exception("Connector job listener failed for job {}", job.id)


def _dead(db: AsyncSession, job: ConnectorJob) -> None:
    job.status = "dead"
    job.next_attempt_at = None
    emit(
        db,
        ConnectorJobFailed(
            tenant_id=job.tenant_id,
            job_id=job.id,
            category=job.category,
            op=job.op,
            subject_kind=job.subject_kind,
            subject_id=job.subject_id,
        ),
    )


async def execute(ctx: RequestContext, db: AsyncSession, job_id: uuid.UUID) -> tuple[str, int | None]:
    """Run one attempt; returns ``(status, retry_in_seconds)`` -- status ``"deferred"`` for a reschedule that did not
    count an attempt (kill switch, rate limit; the row keeps its status).

    Epic 66: while the Ecosystem Kill Switch is on the job is rescheduled (+5 min) without counting the attempt;
    ``ConnectorRateLimited`` reschedules after ``retry_after`` (not counted); ``ConnectorAuthError`` /
    ``ConnectorPermanentError`` end the job ``dead`` at once; ops with a registered runner (``sync:``) run it instead
    of ``adapter.handle``. Jobs of an Installation notify the registered job listeners."""
    job = (
        await db.execute(select(ConnectorJob).where(ConnectorJob.id == job_id).with_for_update(skip_locked=True))
    ).scalar_one_or_none()
    if job is None or job.status in ("succeeded", "dead", "running"):
        return (job.status if job is not None else "missing"), None
    if await ecosystem_suspended(job.tenant_id):
        job.next_attempt_at = _now() + timedelta(seconds=SUSPENDED_RETRY_SECONDS)
        await db.flush()
        return DEFERRED, SUSPENDED_RETRY_SECONDS
    job.status = "running"
    job.attempts += 1
    await db.flush()
    config_row = await db.get(ProviderConnectorConfig, job.config_id) if job.config_id else None
    outcome, error = "succeeded", None
    try:
        runner = registry.job_runner(job.op)
        if runner is not None:
            result = await runner(ctx, db, job)
        else:
            adapter = await registry.build_for(ctx, db, job.category, config_row)
            result = await adapter.handle(job.op, dict(job.payload or {}))
        job.status = "succeeded"
        job.result = dict(result or {})
        job.last_error = None
        job.next_attempt_at = None
    except ManualActionRequired as manual:
        outcome = "manual"
        job.status = "succeeded"
        job.result = {"manual": True, "instructions": manual.instructions, **manual.details}
        job.next_attempt_at = None
    except ConnectorRateLimited as exc:
        outcome, error = "rate_limited", exc
        delay = int(exc.retry_after)
        job.attempts = max(job.attempts - 1, 0)
        job.status = "failed"
        job.last_error = f"rate_limited: retry after {delay} s"
        job.next_attempt_at = _now() + timedelta(seconds=delay)
        await db.flush()
        await _notify(ctx, db, job, outcome, error)
        return DEFERRED, delay
    except (ConnectorAuthError, ConnectorPermanentError) as exc:
        outcome, error = ("auth_failed" if isinstance(exc, ConnectorAuthError) else "permanent"), exc
        logger.warning("Connector job {} failed permanently: {}", job.id, exc.code)
        job.last_error = f"{exc.code}: {exc}"[:MAX_ERROR]
        _dead(db, job)
    except Exception as exc:  # provider errors: bounded retry
        logger.warning("Connector job {} attempt {} failed: {}", job.id, job.attempts, type(exc).__name__)
        job.last_error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR]
        error = exc
        if job.attempts >= job.max_attempts:
            outcome = "dead"
            _dead(db, job)
        else:
            delay = BACKOFF_BASE_SECONDS * 2 ** (job.attempts - 1)
            job.status = "failed"
            job.next_attempt_at = _now() + timedelta(seconds=delay)
            await db.flush()
            await _notify(ctx, db, job, "retry", exc)
            return job.status, delay
    await db.flush()
    await _notify(ctx, db, job, outcome, error)
    return job.status, None


async def process_inbound(ctx: RequestContext, db: AsyncSession, event_id: uuid.UUID) -> str:
    event = (
        await db.execute(select(ConnectorInboundEvent).where(ConnectorInboundEvent.id == event_id).with_for_update())
    ).scalar_one_or_none()
    if event is None or event.status != "received":
        return event.status if event is not None else "missing"
    handler = registry.webhook_handler(event.category)
    config_row = await db.get(ProviderConnectorConfig, event.config_id)
    try:
        if handler is not None and config_row is not None:
            await handler(ctx, db, dict(event.payload or {}), await registry.load_config(db, config_row))
        event.status = "processed"
        event.processed_at = _now()
    except Exception as exc:
        logger.warning("Connector inbound event {} failed: {}", event.id, type(exc).__name__)
        event.status = "failed"
        event.error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR]
    await db.flush()
    return event.status


# ---------------------------------------------------------------- after-commit wiring
def _on_queued(event: ConnectorJobQueued) -> None:
    from app.tasks import connectors as connector_tasks
    from app.tasks.runtime import enqueue as enqueue_task

    if registry.job_runner(event.op) is not None:  # Epic 66: sync jobs run on the ``ecosystem`` queue
        from app.tasks import integrations as integration_tasks

        enqueue_task(integration_tasks.run_sync_job, str(event.tenant_id), str(event.job_id))
        return
    enqueue_task(connector_tasks.run_job, str(event.tenant_id), str(event.job_id))


subscribe(ConnectorJobQueued, _on_queued)
