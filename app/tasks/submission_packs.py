# ruff: file-ignore[line-too-long]
"""Submission pack follow-ups (Epic 61). Every task starts with ``tenant_id``. Queue ``maintenance``.

* ``submission_packs.link_submission`` -- a person created a Submission: the open pack becomes ``used`` (+ diff).
* ``submission_packs.discard_for_application`` -- the Application left Shortlisted without a submission.
* ``submission_packs.recheck_candidate`` -- ``client_submission`` consent changed: re-run open packs' checks.
* ``submission_packs.checks_only`` -- a Submission Agent run failed / stopped: a checks-only pack (no model call).
"""

import uuid

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import run

QUEUE = "maintenance"


@celery_app.task(name="submission_packs.link_submission", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def link_submission(tenant_id: str, submission_id: str) -> bool:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> bool:
        from app.services.submission_packs import service as svc

        async with tenant_session(tid) as db:
            pack = await svc.link_submission(RequestContext(tenant_id=tid), db, uuid.UUID(submission_id))
        return pack is not None

    return bool(run(job, tenant_id=tid))


@celery_app.task(name="submission_packs.discard_for_application", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def discard_for_application(tenant_id: str, application_id: str, reason: str) -> bool:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> bool:
        from app.services.submission_packs import service

        async with tenant_session(tid) as db:
            return await service.discard_for_application(db, uuid.UUID(application_id), reason)

    return bool(run(job, tenant_id=tid))


@celery_app.task(name="submission_packs.recheck_candidate", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def recheck_candidate(tenant_id: str, candidate_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.submission_packs import service as svc

        async with tenant_session(tid) as db:
            return await svc.recheck_for_candidate(RequestContext(tenant_id=tid), db, uuid.UUID(candidate_id))

    return int(run(job, tenant_id=tid))


@celery_app.task(name="submission_packs.checks_only", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def checks_only(tenant_id: str, application_id: str, run_id: str | None = None) -> bool:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> bool:
        from loguru import logger

        from app.core.errors import DomainError
        from app.services.submission_packs import service as svc

        ctx = RequestContext(tenant_id=tid, agent_run_id=uuid.UUID(run_id) if run_id else None)
        async with tenant_session(tid) as db:
            try:
                pack = await svc.prepare(ctx, db, uuid.UUID(application_id), write_up_failed=True)
            except DomainError as exc:
                logger.info("checks-only submission pack skipped ({}): {}", application_id, exc.code)
                return False
            if run_id and pack.agent_run_id is None:
                pack.agent_run_id = uuid.UUID(run_id)
        return True

    return bool(run(job, tenant_id=tid))


__all__ = ["QUEUE", "checks_only", "discard_for_application", "link_submission", "recheck_candidate"]
