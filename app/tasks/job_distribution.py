# ruff: file-ignore[line-too-long]
"""Job board distribution tasks (Epic 70), queue ``ecosystem``. Every task starts with ``tenant_id`` (beat fan-outs
excepted).

* ``job_distribution.send_notice(tenant_id, application_id, board_key)`` -- the ``job_board_application`` notice
  email to an ingested applicant (outbox mailer).
* ``job_distribution.suppress_withdrawn(tenant_id, candidate_id)`` -- ``data_processing`` withdrawn -> suppression
  hashes (``consent_withdrawn``).
* Beat: ``job_distribution.expiry_scan`` (daily 08:30 IST) and ``job_distribution.applicant_digest`` (daily 09:00 IST).
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "ecosystem"


@celery_app.task(name="job_distribution.send_notice", acks_late=True, queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def send_notice(tenant_id: str, application_id: str, board_key: str) -> str:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> str:
        from app.services.job_distribution import notices

        return await notices.send_notice(tid, uuid.UUID(str(application_id)), board_key)

    return str(run(job, tenant_id=tid))


@celery_app.task(name="job_distribution.suppress_withdrawn", acks_late=True, queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def suppress_withdrawn(tenant_id: str, candidate_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.core import audit
        from app.services.job_distribution import suppression

        async with tenant_session(tid) as db:
            audit.set_actor(db, "system", None)
            return await suppression.add_candidate(db, uuid.UUID(str(candidate_id)), "consent_withdrawn")

    return int(run(job, tenant_id=tid))


async def _expiry(tid: uuid.UUID) -> Any:
    from app.core import audit
    from app.services.job_distribution import expiry

    async with tenant_session(tid) as db:
        audit.set_actor(db, "system", None)
        return await expiry.scan(db)


@celery_app.task(name="job_distribution.expiry_scan", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def expiry_scan() -> Any:
    return run(lambda: for_each_tenant(_expiry))


async def _digest(tid: uuid.UUID) -> Any:
    from app.services.job_distribution import notices

    return await notices.applicant_digest(tid)


@celery_app.task(name="job_distribution.applicant_digest", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def applicant_digest() -> Any:
    return run(lambda: for_each_tenant(_digest))
