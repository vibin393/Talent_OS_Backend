# ruff: noqa: E501
"""Epic 63 Billing / Collections Agent tasks: the hourly ``collection_rules.daily_tick`` and
``collection_rules.billing_prep_tick`` (``maintenance``), ``collection_rules.ptp_broken`` and the collection log rows of
collections Proposals (``proposal_created`` / ``proposal_decided``).
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "maintenance"


@celery_app.task(name="collection_rules.daily_tick", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def collections_tick() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> Any:
        from app.services.collection_rules import ticks

        async with tenant_session(tid) as db:
            return await ticks.daily_tick(db)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


@celery_app.task(name="collection_rules.billing_prep_tick", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def billing_prep_tick() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> Any:
        from app.services.collection_rules import ticks

        async with tenant_session(tid) as db:
            return await ticks.billing_prep_tick(db)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


@celery_app.task(name="collection_rules.ptp_broken", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def ptp_broken(tenant_id: str, client_id: str, promise_id: str) -> str | None:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> str | None:
        from app.services.collection_rules import ticks

        async with tenant_session(tid) as db:
            return await ticks.start_ptp_run(db, uuid.UUID(client_id), uuid.UUID(promise_id))

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="collection_rules.proposal_created", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def proposal_created(tenant_id: str, proposal_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.collection_rules import log

        async with tenant_session(tid) as db:
            return await log.on_proposal_created(db, uuid.UUID(proposal_id))

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="collection_rules.proposal_decided", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def proposal_decided(tenant_id: str, proposal_id: str, outcome: str, decided_by: str | None) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.collection_rules import log

        async with tenant_session(tid) as db:
            return await log.on_proposal_decided(
                db, uuid.UUID(proposal_id), outcome, uuid.UUID(decided_by) if decided_by else None
            )

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


__all__ = ["billing_prep_tick", "collections_tick", "proposal_created", "proposal_decided", "ptp_broken"]
