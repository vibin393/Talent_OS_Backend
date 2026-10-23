# ruff: file-ignore[line-too-long]
"""Interview Assistant tasks (Epic 60). Every task starts with ``tenant_id`` (the beat fan-out excepted). Queue
``maintenance``.

* ``interview_captures.withdraw`` -- purge one capture's recording, transcript and outputs (withdrawal, A7).
* ``interview_captures.withdraw_for_candidate`` -- ``ConsentWithdrawn(interview_recording)``: every active capture of the
  Candidate.
* ``interview_captures.on_scorecard_submitted`` -- record the draft's suggestion decisions.
* ``interview_captures.on_interview_cancelled`` -- cancel an active assistant run of the Interview.
* ``interview_captures.on_run_ended`` -- a failed / stopped run without outputs leaves the capture re-runnable.
* ``interview_captures.sweep`` (beat, hourly) -- the 24 h withdrawal SLA.
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "maintenance"


@celery_app.task(name="interview_captures.withdraw", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def withdraw(tenant_id: str, capture_id: str, reason: str) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> dict[str, int]:
        from app.services.interview_captures import withdrawal

        async with tenant_session(tid) as db:
            return await withdrawal.withdraw(db, uuid.UUID(capture_id), reason=reason)

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="interview_captures.withdraw_for_candidate", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def withdraw_for_candidate(tenant_id: str, candidate_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.interview_captures import withdrawal

        async with tenant_session(tid) as db:
            return await withdrawal.for_candidate(db, uuid.UUID(candidate_id), reason="consent_withdrawn")

    return int(run(job, tenant_id=tid))


@celery_app.task(name="interview_captures.on_scorecard_submitted", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def on_scorecard_submitted(tenant_id: str, scorecard_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.interview_captures import drafts

        async with tenant_session(tid) as db:
            return await drafts.on_scorecard_submitted(db, uuid.UUID(scorecard_id))

    return int(run(job, tenant_id=tid))


@celery_app.task(name="interview_captures.on_interview_cancelled", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def on_interview_cancelled(tenant_id: str, interview_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.interview_captures import common, withdrawal

        async with tenant_session(tid) as db:
            capture = await common.capture_for_interview(db, uuid.UUID(interview_id))
            if capture is None or capture.agent_run_id is None:
                return 0
            await withdrawal._cancel_run(db, capture)
            return 1

    return int(run(job, tenant_id=tid))


@celery_app.task(name="interview_captures.on_run_ended", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def on_run_ended(tenant_id: str, interview_id: str, run_id: str, status: str, reason_code: str | None) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.interview_captures import common, outputs

        async with tenant_session(tid) as db:
            capture = await common.capture_for_interview(db, uuid.UUID(interview_id), for_update=True)
            if capture is None or capture.status != "processing":
                return 0
            capture.agent_run_id = uuid.UUID(run_id)
            if await outputs.current(db, capture.id) is None:
                capture.status = "uploaded"
                capture.failure_code = (
                    reason_code or ("no_outputs" if status == "completed" else status) or "failed"
                )[:40]
            else:
                capture.status = "ready"
            await db.flush()
            return 1

    return int(run(job, tenant_id=tid))


@celery_app.task(name="interview_captures.sweep")  # type: ignore[misc,untyped-decorator,unused-ignore]
def sweep() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> int:
        from app.services.interview_captures import withdrawal

        async with tenant_session(tid) as db:
            return await withdrawal.sweep(db)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


__all__ = [
    "on_interview_cancelled",
    "on_run_ended",
    "on_scorecard_submitted",
    "sweep",
    "withdraw",
    "withdraw_for_candidate",
]
