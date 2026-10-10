"""Platform-operations Celery tasks (Epic 12).

* ``expire_support_grants()`` -- every 5 min.
* ``export_tenant(tenant_id, export_id)`` -- offboarding export.
* ``process_tenant_deletions()`` -- daily, on ``DATABASE_OWNER_URL``.
* ``notify_budget_alert(tenant_id, threshold, period, used, budget)`` -- once per threshold per month.
* ``resume_budget_paused(tenant_id | None)`` -- when a budget is raised and 00:05 UTC on the 1st.
* ``send_incident_emails(tenant_id, emails, subject, body)``.
"""

import uuid

from loguru import logger
from sqlalchemy import select

from app.core.celery import celery_app
from app.core.database import platform_session, tenant_session
from app.core.mailer import EmailMessage, send_email
from app.tasks.ops_runtime import enqueue, for_each_tenant, run

QUEUE = "maintenance"  # Epic 13 (AD-37): the queue of every task in this module


@celery_app.task(name="platform.expire_support_grants")  # type: ignore[untyped-decorator,unused-ignore]
def expire_support_grants(tenant_id: str | None = None) -> dict[str, object]:
    from app.services import support_access_service

    async def one(tid: uuid.UUID) -> int:
        async with tenant_session(tid) as db:
            return await support_access_service.expire_grants(db)

    return run(lambda: for_each_tenant(one, ("active", "suspended", "offboarding"), only=tenant_id))  # type: ignore[no-any-return]


@celery_app.task(name="platform.export_tenant", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def export_tenant(tenant_id: str, export_id: str) -> str:
    from app.services import tenant_lifecycle_service

    tid, eid = uuid.UUID(tenant_id), uuid.UUID(export_id)
    return run(lambda: tenant_lifecycle_service.export_tenant(tid, eid), tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="platform.process_tenant_deletions")  # type: ignore[untyped-decorator,unused-ignore]
def process_tenant_deletions() -> list[str]:
    from app.services import tenant_lifecycle_service

    return [str(t) for t in run(tenant_lifecycle_service.process_deletions)]


@celery_app.task(name="platform.notify_budget_alert")  # type: ignore[untyped-decorator,unused-ignore]
def notify_budget_alert(tenant_id: str, threshold: int, period: str, used: int, budget: int) -> int:
    from app.models.identity import PlatformOperator
    from app.services import identity_bridge
    from app.services.privacy.common import email_users

    tid = uuid.UUID(tenant_id)

    async def job() -> int:
        async with tenant_session(tid) as db:
            admins = await identity_bridge.tenant_admins(db)
        async with platform_session() as pdb:
            operators = [
                {"email": e}
                for e in (await pdb.execute(select(PlatformOperator.email).where(PlatformOperator.is_active.is_(True)))).scalars()
            ]
            tenant = await identity_bridge.get_tenant(pdb, tid)
        behaviour = {
            80: "No action is needed yet.",
            100: "Bulk AI jobs are paused; interactive AI continues until 120 %.",
            120: "All AI calls are refused until the budget is raised or the month ends.",
        }[threshold]
        text = (
            f"AI usage of {tenant.name if tenant else tenant_id} reached {threshold} % of its monthly token budget "
            f"({used} of {budget} tokens, {period}). {behaviour}\n"
        )
        return await email_users(
            [*admins, *operators], f"AI budget {threshold} % reached", text, tenant_id=tid, kind="ai_budget_alert"
        )

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="platform.resume_budget_paused")  # type: ignore[untyped-decorator,unused-ignore]
def resume_budget_paused(tenant_id: str | None = None) -> dict[str, object]:
    from app.services.candidates import imports

    async def one(tid: uuid.UUID) -> int:
        async with tenant_session(tid) as db:
            return await imports.resume_budget_paused(db, tid)

    if tenant_id is not None:
        tid = uuid.UUID(tenant_id)
        return {tenant_id: run(lambda: one(tid), tenant_id=tid)}
    return run(lambda: for_each_tenant(one))  # type: ignore[no-any-return]


@celery_app.task(name="platform.send_incident_emails", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def send_incident_emails(tenant_id: str, emails: list[str], subject: str, body: str) -> int:
    tid = uuid.UUID(tenant_id)

    async def job() -> int:
        sent = 0
        for email in emails:
            try:
                await send_email(EmailMessage(to=email, subject=subject, text=body, tenant_id=tid, kind="incident_notice"))
                sent += 1
            except Exception:
                logger.exception("Incident email failed (tenant {})", tid)
        return sent

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="platform.notify_sub_processor", acks_late=True)  # type: ignore[untyped-decorator,unused-ignore]
def notify_sub_processor(sub_id: str, tenant_id: str | None = None) -> dict[str, object]:
    from app.services import platform_service

    sid = uuid.UUID(sub_id)

    async def one(tid: uuid.UUID) -> int:
        return await platform_service.email_sub_processor_notice(tid, sid)

    return run(lambda: for_each_tenant(one, only=tenant_id))  # type: ignore[no-any-return]


# ---------------------------------------------------------------- enqueue helpers used by services
def enqueue_sub_processor_notice(sub_id: uuid.UUID) -> None:
    enqueue(notify_sub_processor, str(sub_id))


def enqueue_resume_budget_paused(tenant_id: uuid.UUID) -> None:
    enqueue(resume_budget_paused, str(tenant_id))


def enqueue_incident_emails(tenant_id: uuid.UUID, emails: list[str], subject: str, body: str) -> None:
    enqueue(send_incident_emails, str(tenant_id), emails, subject, body)
