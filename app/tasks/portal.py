"""Portal Celery tasks (Epic 9; AD-9: tenant tasks take ``tenant_id`` first, run in ``tenant_session``,
idempotent / safe to retry).

* ``refresh_posting_search(tenant_id, demand_id)`` -- slug + search vector after ``DemandPublished``.
* ``send_apply_confirmation(tenant_id, application_id, already_applied)`` -- "We received your application".
* ``send_candidate_sign_in_link(tenant_id, candidate_id, email)`` -- magic link (after the 202).
* ``build_candidate_export(tenant_id, job_id)`` -- self-service data export zip + secure link email.
* ``notify_privacy_contact(tenant_id, dsr_id)`` -- a portal correction / erasure request was filed.
* Beat: ``purge_apply_sessions`` (hourly), ``expire_exports`` (hourly), ``alert_stale_exports`` (hourly).
"""

import uuid

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.services import portal
from app.tasks.ops_runtime import for_each_tenant, run

QUEUE = "interactive"  # Epic 13 (AD-37): the queue of every task in this module


@celery_app.task(name="portal.refresh_posting_search", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def refresh_posting_search(tenant_id: str, demand_id: str) -> str:
    tid, did = uuid.UUID(tenant_id), uuid.UUID(demand_id)

    async def job() -> str:
        async with tenant_session(tid) as db:
            await portal.careers.refresh_posting_search(db, did)
        return "refreshed"

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="portal.send_apply_confirmation", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def send_apply_confirmation(tenant_id: str, application_id: str, already_applied: bool = False) -> str:
    tid, aid = uuid.UUID(tenant_id), uuid.UUID(application_id)
    return run(lambda: portal.apply.send_confirmation(tid, aid, already_applied), tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="portal.send_candidate_sign_in_link", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def send_candidate_sign_in_link(tenant_id: str, candidate_id: str, email: str) -> str:
    tid, cid = uuid.UUID(tenant_id), uuid.UUID(candidate_id)
    return run(lambda: portal.candidate_auth.send_sign_in_link(tid, cid, email), tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="portal.build_candidate_export", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def build_candidate_export(tenant_id: str, job_id: str) -> str:
    tid, jid = uuid.UUID(tenant_id), uuid.UUID(job_id)
    return run(lambda: portal.privacy_self_service.build_export(tid, jid), tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="portal.notify_privacy_contact", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def notify_privacy_contact(tenant_id: str, dsr_id: str) -> str:
    tid, did = uuid.UUID(tenant_id), uuid.UUID(dsr_id)
    return run(lambda: portal.privacy_self_service.notify_privacy_contact(tid, did), tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="portal.purge_apply_sessions")  # type: ignore[untyped-decorator,unused-ignore]
def purge_apply_sessions() -> dict[str, object]:
    return run(
        lambda: for_each_tenant(portal.apply.purge_expired, statuses=("active", "suspended", "offboarding"))
    )  # type: ignore[no-any-return]


@celery_app.task(name="portal.expire_exports")  # type: ignore[untyped-decorator,unused-ignore]
def expire_exports() -> dict[str, object]:
    return run(
        lambda: for_each_tenant(
            portal.privacy_self_service.expire_exports, statuses=("active", "suspended", "offboarding")
        )
    )  # type: ignore[no-any-return]


@celery_app.task(name="portal.alert_stale_exports")  # type: ignore[untyped-decorator,unused-ignore]
def alert_stale_exports() -> dict[str, object]:
    return run(lambda: for_each_tenant(portal.privacy_self_service.alert_stale_exports))  # type: ignore[no-any-return]
