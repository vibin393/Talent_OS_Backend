# ruff: file-ignore[line-too-long]
"""Voice Screening tasks (Epic 58). Every task starts with ``tenant_id`` (the beat fan-out excepted).

* ``voice.run_fake_call`` (``connectors``) -- play the Fake adapter's deterministic call lifecycle.
* ``voice.purge_recordings`` (``maintenance``) -- ``ConsentWithdrawn(call_recording | voice_screening)``: purge the
  Candidate's recordings (and, for ``voice_screening``, transcripts and voice answers) immediately.
* ``voice.sweep`` (beat, hourly, ``maintenance``) -- the 24 h withdrawal SLA.
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "maintenance"


@celery_app.task(name="voice.run_fake_call", acks_late=True, queue="connectors")  # type: ignore[misc,untyped-decorator,unused-ignore]
def run_fake_call(tenant_id: str, session_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.voice import provider_jobs

        async with tenant_session(tid) as db:
            return await provider_jobs.run_fake_call(db, uuid.UUID(session_id))

    return int(run(job, tenant_id=tid))


@celery_app.task(name="voice.purge_recordings", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_recordings(tenant_id: str, candidate_id: str, purpose: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.voice import withdrawal

        async with tenant_session(tid) as db:
            return await withdrawal.for_candidate(db, uuid.UUID(candidate_id), purpose)

    return int(run(job, tenant_id=tid))


@celery_app.task(name="voice.on_run_failed", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def on_run_failed(tenant_id: str, run_id: str) -> int:
    """Hand a failed processing run's session to the recruiter; it leaves the transcript with the recruiter (``ready_for_review``, manual answers)."""
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from sqlalchemy import select

        from app.core.context import RequestContext
        from app.models.voice import VoiceSession
        from app.services.voice import agent_ops

        async with tenant_session(tid) as db:
            row = (
                await db.execute(
                    select(VoiceSession)
                    .where(VoiceSession.agent_run_id == uuid.UUID(run_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status != "processing":
                return 0
            row.failure_code = "agent_failed"
            await agent_ops.hand_to_reviewer(RequestContext(tenant_id=tid, actor_kind="system"), db, row)
            return 1

    return int(run(job, tenant_id=tid))


@celery_app.task(name="voice.sweep")  # type: ignore[misc,untyped-decorator,unused-ignore]
def sweep() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> int:
        from app.services.voice import withdrawal

        async with tenant_session(tid) as db:
            return await withdrawal.sweep(db)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


__all__ = ["purge_recordings", "run_fake_call", "sweep"]
