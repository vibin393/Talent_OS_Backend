# ruff: file-ignore[line-too-long]
"""Telephony tasks (Epic 76, AD-92), queue ``telephony`` (worker ``worker-telephony``, concurrency 4). Every task starts
with ``tenant_id`` (the beat fan-outs excepted).

* ``telephony.fetch_recording(tenant_id, call_id, attempt)`` -- retrieve a gate-approved recording (5 attempts with
  back-off, then ``failed`` + the P4 manual fallback).
* ``telephony.delete_provider_recording(tenant_id, call_id)`` -- delete the provider copy (hourly retries up to 72 h).
* ``telephony.transcribe(tenant_id, job_id, attempt)`` -- run one STT job (5 attempts).
* ``telephony.withdraw(tenant_id, candidate_id)`` -- withdrawal / erasure: delete every provider copy.
* Beat (``maintenance``): ``telephony.retry_provider_deletes`` (hourly), ``telephony.reconcile_calls`` (15 min).
"""

import uuid
from typing import Any

from loguru import logger

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "telephony"
MAX_ATTEMPTS = 5


def _ctx(tid: uuid.UUID) -> Any:
    from app.core.context import RequestContext

    return RequestContext(tenant_id=tid, actor_kind="system")


def _requeue(task: Any, args: tuple[Any, ...], attempt: int) -> None:
    if celery_app.conf.task_always_eager:
        return  # tests drive retries explicitly
    try:
        task.apply_async(args, countdown=min(30 * 2 ** (attempt - 1), 3600), queue=QUEUE)
    except Exception:  # pragma: no cover - broker down
        logger.exception("Could not re-queue telephony task {}", task.name)


@celery_app.task(name="telephony.fetch_recording", acks_late=True, queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def fetch_recording(tenant_id: str, call_id: str, attempt: int = 1) -> str:
    tid, cid = uuid.UUID(str(tenant_id)), uuid.UUID(str(call_id))

    async def job() -> str:
        from app.services.telephony import recordings

        try:
            async with tenant_session(tid) as db:
                return await recordings.fetch(_ctx(tid), db, cid)
        except Exception as exc:
            logger.warning("Recording fetch for call {} attempt {} failed: {}", cid, attempt, type(exc).__name__)
            if attempt >= MAX_ATTEMPTS:
                async with tenant_session(tid) as db:
                    await recordings.mark_fetch_failed(db, cid)
                return "failed"
            return "retry"

    status = str(run(job, tenant_id=tid))
    if status == "retry":
        _requeue(fetch_recording, (tenant_id, call_id, attempt + 1), attempt)
    return status


@celery_app.task(name="telephony.delete_provider_recording", acks_late=True, queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def delete_provider_recording(tenant_id: str, call_id: str) -> str:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> str:
        from app.services.telephony import recordings

        async with tenant_session(tid) as db:
            return await recordings.delete_provider(_ctx(tid), db, uuid.UUID(str(call_id)))

    return str(run(job, tenant_id=tid))


@celery_app.task(name="telephony.transcribe", acks_late=True, queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def transcribe(tenant_id: str, job_id: str, attempt: int = 1) -> str:
    tid, jid = uuid.UUID(str(tenant_id)), uuid.UUID(str(job_id))

    async def job() -> str:
        from app.services.telephony import stt_jobs

        try:
            async with tenant_session(tid) as db:
                return await stt_jobs.run(_ctx(tid), db, jid)  # counts attempts; "retry" until the 5th failure
        except Exception as exc:
            logger.warning("STT job {} failed: {}", jid, type(exc).__name__)
            if attempt >= MAX_ATTEMPTS:
                async with tenant_session(tid) as db:
                    await stt_jobs.mark_failed(db, jid)
                return "failed"
            return "retry"

    status = str(run(job, tenant_id=tid))
    if status in ("retry", "held"):
        _requeue(transcribe, (tenant_id, job_id, attempt + (1 if status == "retry" else 0)), attempt)
    return status


@celery_app.task(name="telephony.withdraw", acks_late=True, queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def withdraw(tenant_id: str, candidate_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.telephony import recordings

        async with tenant_session(tid) as db:
            ids = await recordings.for_candidate(db, uuid.UUID(str(candidate_id)))
        for cid in ids:
            async with tenant_session(tid) as db:
                await recordings.delete_provider(_ctx(tid), db, cid)
        return len(ids)

    return int(run(job, tenant_id=tid))


@celery_app.task(name="telephony.retry_provider_deletes", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def retry_provider_deletes() -> dict[str, Any]:
    async def per_tenant(tid: uuid.UUID) -> int:
        from app.services.telephony import recordings

        async with tenant_session(tid) as db:
            ids = await recordings.due_deletes(db)
        for cid in ids:
            async with tenant_session(tid) as db:
                await recordings.delete_provider(_ctx(tid), db, cid)
        return len(ids)

    return dict(run(lambda: for_each_tenant(per_tenant)))


@celery_app.task(name="telephony.reconcile_calls", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def reconcile_calls() -> dict[str, Any]:
    async def per_tenant(tid: uuid.UUID) -> int:
        from app.services.telephony import reconcile

        async with tenant_session(tid) as db:
            return await reconcile.stuck_calls(_ctx(tid), db)

    return dict(run(lambda: for_each_tenant(per_tenant)))
