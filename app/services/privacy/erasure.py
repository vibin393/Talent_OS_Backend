"""Erasure runs (Epic 12 A3.3, FR-68, addendum §E). Owns ordering, execution and verification only.

``run_erasure(tenant_id, run_id)``:

1. runs every handler with ``order < 1000`` as ``actor=system``, one transaction each, recording each
   handler's result in ``erasure_runs.handler_results``; a retry re-runs only the handlers that failed;
2. calls ``verify()`` on every handler -- any finding marks the run ``failed`` and alerts the admins;
3. runs the ``order >= 1000`` handlers last (``audit.shred``): audit rows written by the anonymising updates
   carry old values encrypted under the candidate's key, so they read ``[erased]`` too;
4. appends a PII-free line ``{tenant_id, candidate_id, completed_at}`` to
   ``ERASURE_LEDGER_PREFIX/<yyyy-mm>.jsonl`` (replayed by ``python -m app.cli reapply-erasures`` after a
   backup restore);
5. completes the DSR (or marks the retention item ``erased``).
"""

import contextlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, storage
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError
from app.core.events import emit
from app.core.settings import get_settings
from app.events.privacy import ErasureRequested
from app.models.privacy import ErasureRun, RetentionRunItem
from app.schemas.privacy import ErasureRunOut
from app.services.privacy import registry
from app.services.privacy.common import email_users, now, require_tenant

SessionFactory = Callable[[uuid.UUID], contextlib.AbstractAsyncContextManager[AsyncSession]]
FINAL_ORDER = 1000


async def create_run(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    *,
    reason: str,
    dsr_id: uuid.UUID | None = None,
    retention_run_id: uuid.UUID | None = None,
    enqueue: bool = True,
) -> ErasureRun:
    """Create (or reuse the unfinished) erasure run and enqueue it after commit."""
    tenant_id = require_tenant(ctx)
    from app.services.privacy import dsr as dsr_service

    hold = await dsr_service.active_hold(db, candidate_id)
    if hold is not None:
        raise ConflictError(
            "The candidate is under a legal hold", code="legal_hold_active", details={"reason": hold.reason}
        )
    existing = None
    if dsr_id is not None:
        existing = (
            await db.execute(
                select(ErasureRun).where(ErasureRun.dsr_id == dsr_id, ErasureRun.status != "completed")
            )
        ).scalar_one_or_none()
    run = existing or ErasureRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        reason=reason,
        dsr_id=dsr_id,
        retention_run_id=retention_run_id,
        status="pending",
        handler_results={},
    )
    if existing is None:
        db.add(run)
    await db.flush()
    audit.record("erasure.requested", "candidate", candidate_id, session=db, run_id=run.id, reason=reason)
    if enqueue:
        emit(db, ErasureRequested(tenant_id=tenant_id, run_id=run.id))
    return run


def _ledger_key(at: datetime) -> str:
    return f"{get_settings().erasure_ledger_prefix}{at:%Y-%m}.jsonl"


async def append_ledger(tenant_id: uuid.UUID, candidate_id: uuid.UUID, completed_at: datetime) -> None:
    """Append one PII-free line to the monthly ledger (read-modify-write; a retry may duplicate a line,
    which replay tolerates)."""
    key = _ledger_key(completed_at)
    backend = storage.get_storage()
    try:
        existing = await backend.get(key)
    except FileNotFoundError:
        existing = b""
    except Exception as exc:  # S3 NoSuchKey etc.
        if "NoSuchKey" not in type(exc).__name__ and "404" not in str(exc):
            raise
        existing = b""
    line = json.dumps(
        {
            "tenant_id": str(tenant_id),
            "candidate_id": str(candidate_id),
            "completed_at": completed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        },
        separators=(",", ":"),
    )
    await backend.put(key, existing + line.encode("utf-8") + b"\n", "application/x-ndjson")


async def read_ledger(since: date) -> list[dict[str, Any]]:
    """Ledger lines with ``completed_at >= since`` (months from ``since`` to now)."""
    backend = storage.get_storage()
    out: list[dict[str, Any]] = []
    year, month = since.year, since.month
    today = datetime.now(UTC)
    while (year, month) <= (today.year, today.month):
        key = f"{get_settings().erasure_ledger_prefix}{year:04d}-{month:02d}.jsonl"
        try:
            data = await backend.get(key)
        except Exception:  # missing month
            data = b""
        for raw in data.decode("utf-8").splitlines():
            if raw.strip():
                entry = json.loads(raw)
                if entry["completed_at"][:10] >= since.isoformat():
                    out.append(entry)
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out


async def _save(sessions: SessionFactory, tenant_id: uuid.UUID, run_id: uuid.UUID, **values: Any) -> None:
    async with sessions(tenant_id) as db:
        audit.set_actor(db, "system", None)
        run = await db.get(ErasureRun, run_id, with_for_update=True)
        if run is not None:
            for key, value in values.items():
                setattr(run, key, value)


async def _alert_failure(sessions: SessionFactory, tenant_id: uuid.UUID, run: ErasureRun, why: str) -> None:
    from app.services import identity_bridge

    try:
        async with sessions(tenant_id) as db:
            admins = await identity_bridge.users_with_permission(db, "dsr:process")
        await email_users(
            admins,
            "An erasure run needs attention",
            f"Erasure run {run.id} failed ({why}). Open the privacy center to retry it.\n",
            tenant_id=tenant_id,
            kind="erasure_failed",
        )
    except Exception:
        logger.exception("Could not alert admins about erasure run {}", run.id)


async def _run_handlers(
    sessions: SessionFactory,
    ectx: registry.ErasureContext,
    handlers: list[registry.ErasureHandler],
    results: dict[str, Any],
) -> bool:
    ok = True
    for handler in handlers:
        if results.get(handler.name, {}).get("status") == "ok":
            continue  # retry: only failed / not yet run handlers
        try:
            async with sessions(ectx.tenant_id) as db:
                audit.set_actor(db, "system", None)
                counts = await handler.erase(db, ectx)
            results[handler.name] = {"status": "ok", "counts": counts}
        except Exception as exc:
            logger.exception("Erasure handler {} failed (run {})", handler.name, ectx.run_id)
            results[handler.name] = {"status": "failed", "error": type(exc).__name__}
            ok = False
        await _save(sessions, ectx.tenant_id, ectx.run_id, handler_results=dict(results))
    return ok


async def run_erasure(tenant_id: uuid.UUID, run_id: uuid.UUID, *, sessions: SessionFactory = tenant_session) -> str:
    """Execute (or resume) an erasure run. Idempotent; returns the final status."""
    async with sessions(tenant_id) as db:
        audit.set_actor(db, "system", None)
        run = await db.get(ErasureRun, run_id, with_for_update=True)
        if run is None:
            logger.warning("Erasure run {} not found", run_id)
            return "missing"
        if run.status == "completed":
            return "completed"
        from app.services.privacy import dsr as dsr_service

        hold = await dsr_service.active_hold(db, run.candidate_id)
        if hold is not None:
            run.status = "failed"
            run.verification = {"blocked": "legal_hold_active"}
            return "failed"
        run.status = "running"
        run.started_at = run.started_at or now()
        results: dict[str, Any] = dict(run.handler_results or {})
        # Epic 35 (AD-34): rows under an active retention class stay; the run records what was kept and why.
        from app.services.privacy.retention_bindings import retention_for_erasure

        retained, retained_summary = await retention_for_erasure(db, run.candidate_id)
        run.retained_summary = retained_summary
        ectx = registry.ErasureContext(
            tenant_id=tenant_id,
            candidate_id=run.candidate_id,
            run_id=run.id,
            reason=run.reason,  # type: ignore[arg-type]
            retained=retained,
        )
        snapshot = ErasureRun(id=run.id, candidate_id=run.candidate_id, dsr_id=run.dsr_id)
        dsr_id, retention_run_id = run.dsr_id, run.retention_run_id

    handlers = registry.handlers()
    main = [h for h in handlers if h.order < FINAL_ORDER]
    final = [h for h in handlers if h.order >= FINAL_ORDER]

    if not await _run_handlers(sessions, ectx, main, results):
        await _save(sessions, tenant_id, run_id, status="failed", finished_at=now())
        await _alert_failure(sessions, tenant_id, snapshot, "handler failed")
        return "failed"

    findings: dict[str, list[str]] = {}
    async with sessions(tenant_id) as db:
        for handler in main:
            residual = await handler.verify(db, ectx)
            if residual:
                findings[handler.name] = residual
    if findings:
        await _save(sessions, tenant_id, run_id, status="failed", verification={"findings": findings}, finished_at=now())
        await _alert_failure(sessions, tenant_id, snapshot, "verification found residual personal data")
        return "failed"

    if not await _run_handlers(sessions, ectx, final, results):
        await _save(sessions, tenant_id, run_id, status="failed", finished_at=now())
        await _alert_failure(sessions, tenant_id, snapshot, "audit shred failed")
        return "failed"

    completed_at = now()
    await append_ledger(tenant_id, ectx.candidate_id, completed_at)
    async with sessions(tenant_id) as db:
        audit.set_actor(db, "system", None)
        run = await db.get(ErasureRun, run_id, with_for_update=True)
        assert run is not None
        run.status = "completed"
        run.verification = {"findings": {}}
        run.finished_at = completed_at
        if dsr_id is not None:
            from app.services.privacy import dsr as dsr_service

            await dsr_service.complete_system(
                db, dsr_id, {"erasure_run_id": str(run_id), "retained": retained_summary}
            )
        if retention_run_id is not None:
            item = (
                await db.execute(
                    select(RetentionRunItem).where(
                        RetentionRunItem.run_id == retention_run_id, RetentionRunItem.candidate_id == ectx.candidate_id
                    )
                )
            ).scalar_one_or_none()
            if item is not None:
                item.status = "erased"
        audit.record("erasure.completed", "erasure_run", run_id, session=db)
    return "completed"


async def get_run(db: AsyncSession, run_id: uuid.UUID) -> ErasureRun | None:
    return await db.get(ErasureRun, run_id)


async def run_out(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> ErasureRunOut:  # noqa: ARG001
    run = await db.get(ErasureRun, run_id)
    if run is None:
        from app.core.errors import NotFound

        raise NotFound("Erasure run not found")
    return ErasureRunOut.model_validate(run)


async def retry(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> ErasureRunOut:
    """Re-enqueue a failed run: only the handlers that failed (or never ran) execute again."""
    run = await db.get(ErasureRun, run_id, with_for_update=True)
    if run is None:
        from app.core.errors import NotFound

        raise NotFound("Erasure run not found")
    if run.status != "failed":
        raise ConflictError("Only a failed run can be retried", code="invalid_transition")
    run.status = "pending"
    await db.flush()
    audit.record("erasure.retried", "erasure_run", run.id, session=db)
    emit(db, ErasureRequested(tenant_id=run.tenant_id, run_id=run.id))
    return await run_out(ctx, db, run_id)
