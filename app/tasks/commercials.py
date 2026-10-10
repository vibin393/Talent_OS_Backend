"""Commercials Celery tasks (Epic 16; queue ``maintenance``). Every task starts with ``tenant_id`` (except fan-outs).

* ``commercials.daily_all`` (beat, daily 01:30 IST) -> ``commercials.daily(tenant_id)``: expire contracts per
  Legal Entity local date, renewal reminders (deduped), PO expiry alerts and validity events.
* ``commercials.signature_completed(tenant_id, contract_id, request_id, document_id)``: the e-sign completion of
  a contract (store the signed file, attempt activation as the system).
"""  # noqa: E501

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "maintenance"


async def _daily(tenant_id: uuid.UUID) -> dict[str, Any]:
    from app.services.commercials import contracts, po_jobs

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        expired = await contracts.expire_due(ctx, db)
        reminders = await contracts.renewal_reminders(ctx, db)
        pos = await po_jobs.run_daily(ctx, db)
    return {"expired": expired, "reminders": reminders, **pos}


@celery_app.task(name="commercials.daily", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def daily(tenant_id: str) -> dict[str, Any]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _daily(tid), tenant_id=tid))


@celery_app.task(name="commercials.daily_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def daily_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(daily, str(tid))
    return len(ids)


async def _signature_completed(
    tenant_id: uuid.UUID, contract_id: uuid.UUID, request_id: uuid.UUID, document_id: uuid.UUID
) -> bool:
    from sqlalchemy import text

    from app.services.commercials import contracts

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        file_id = None
        try:
            async with db.begin_nested():
                file_id = (
                    await db.execute(
                        text(
                            "SELECT v.file_id FROM document_versions v WHERE v.document_id = :d "
                            "ORDER BY v.version_no DESC LIMIT 1"
                        ),
                        {"d": document_id},
                    )
                ).scalar_one_or_none()
        except Exception:
            file_id = None
        return await contracts.on_signature_completed(ctx, db, contract_id, request_id, file_id)


@celery_app.task(name="commercials.signature_completed", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def signature_completed(tenant_id: str, contract_id: str, request_id: str, document_id: str) -> bool:
    tid = uuid.UUID(str(tenant_id))
    return bool(
        run(
            lambda: _signature_completed(
                tid, uuid.UUID(contract_id), uuid.UUID(request_id), uuid.UUID(document_id)
            ),
            tenant_id=tid,
        )
    )
