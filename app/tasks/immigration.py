# ruff: noqa: E501
"""Immigration Celery tasks (Epic 46). Queue ``maintenance`` (AD-54). Every task starts with ``tenant_id`` (except
the beat fan-out).

* ``immigration.link_credential(tenant_id, credential_id)``: P2 ``CredentialRecorded`` of a ``permit.*`` type ->
  ``permits.credential_id`` back-link.
* ``immigration.overdue_scan_all`` (beat, 07:00 IST) -> ``immigration.overdue_scan(tenant_id)``: one
  ``immigration_case_step`` (``overdue``) notification per overdue open Case.
"""

import uuid

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "maintenance"


async def _link(tid: uuid.UUID, credential_id: uuid.UUID) -> bool:
    from app.services.immigration import permits

    async with tenant_session(tid) as db:
        return await permits.link_credential(db, credential_id)


@celery_app.task(name="immigration.link_credential", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def link_credential(tenant_id: str, credential_id: str) -> bool:
    tid = uuid.UUID(str(tenant_id))
    return bool(run(lambda: _link(tid, uuid.UUID(str(credential_id))), tenant_id=tid))


async def scan_overdue(tid: uuid.UUID) -> int:
    from sqlalchemy import select

    from app.models.immigration import ImmigrationCase
    from app.services.immigration import cases
    from app.services.immigration.common import OPEN_STATUSES

    async with tenant_session(tid) as db:
        rows = list(
            (
                await db.execute(select(ImmigrationCase).where(ImmigrationCase.status.in_(OPEN_STATUSES)))
            ).scalars()
        )
        n = 0
        for c in rows:
            _, d = await cases.pinned(db, c)
            if cases.overdue(c, d):
                cases.emit_step(db, c, "overdue")
                n += 1
        return n


@celery_app.task(name="immigration.overdue_scan", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def overdue_scan(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: scan_overdue(tid), tenant_id=tid) or 0)


@celery_app.task(name="immigration.overdue_scan_all", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def overdue_scan_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(overdue_scan, str(tid))
    return len(ids)
