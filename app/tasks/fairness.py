# ruff: file-ignore[line-too-long]
"""Bias audit Celery tasks (Epic 49). Queue ``analytics`` (AD-54); the publication reminder runs on ``maintenance``.

* ``fairness.run_report(tenant_id, run_id)`` -- builds one aggregate-only snapshot (``services.fairness.reports``);
  per-tenant concurrency 1 (transaction advisory lock; a locked run is retried in 60 s). Logs counts only.
* ``fairness.delete_self_id(tenant_id, candidate_id)`` -- ``ConsentWithdrawn(demographic_self_id)``: deletes the
  answers unless a newer active consent exists.
* ``fairness.publication_reminders`` (beat, daily) -> ``fairness.publication_reminders_tenant(tenant_id)``.
"""

import uuid
from typing import Any

from loguru import logger

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "analytics"


async def run_report_job(tid: uuid.UUID, run_id: uuid.UUID) -> str:
    from app.core import audit
    from app.services.fairness import reports

    async with tenant_session(tid) as db:
        audit.set_actor(db, "system", None)
        return await reports.run_report_job(db, run_id)


@celery_app.task(name="fairness.run_report", queue=QUEUE, acks_late=True, bind=True, max_retries=30)  # type: ignore[misc,untyped-decorator,unused-ignore]
def run_report(self: Any, tenant_id: str, run_id: str) -> str:
    tid = uuid.UUID(str(tenant_id))
    result = str(run(lambda: run_report_job(tid, uuid.UUID(str(run_id))), tenant_id=tid))
    if result == "locked":
        logger.info("fairness: another report is running for tenant {}; retrying", tid)
        raise self.retry(countdown=60)
    return result


async def delete_self_id_job(tid: uuid.UUID, cid: uuid.UUID) -> int:
    from app.core import audit
    from app.services import privacy
    from app.services.fairness import self_id
    from app.services.fairness.common import CONSENT_PURPOSE

    async with tenant_session(tid) as db:
        audit.set_actor(db, "system", None)
        if await privacy.consent.has_active_consent(db, cid, CONSENT_PURPOSE):
            return 0  # re-granted since the withdrawal: keep the newer answers
        return await self_id.delete_for_candidate(db, [cid], reason="consent_withdrawn")


@celery_app.task(name="fairness.delete_self_id", queue="maintenance", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def delete_self_id(tenant_id: str, candidate_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: delete_self_id_job(tid, uuid.UUID(str(candidate_id))), tenant_id=tid) or 0)


async def reminders_job(tid: uuid.UUID) -> int:
    from app.core import audit
    from app.services import notifications_service as ns
    from app.services.fairness import hooks, publications
    from app.services.fairness.common import now

    async with tenant_session(tid) as db:
        audit.set_actor(db, "system", None)
        due = await publications.due_reminders(db)
        for pub in due:
            recipients = await hooks._compliance(db, {})
            if recipients:
                await ns.deliver(
                    db,
                    "fairness.publication_due",
                    recipients,
                    await hooks._render_due(db, {}),
                    event_id=f"fairness_publication_due:{pub.id}",
                )
            pub.reminded_at = now()
        await db.flush()
        return len(due)


@celery_app.task(name="fairness.publication_reminders_tenant", queue="maintenance", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def publication_reminders_tenant(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: reminders_job(tid), tenant_id=tid) or 0)


@celery_app.task(name="fairness.publication_reminders", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def publication_reminders() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(publication_reminders_tenant, str(tid))
    return len(ids)


FAN_OUT_TASKS = ("fairness.publication_reminders",)
