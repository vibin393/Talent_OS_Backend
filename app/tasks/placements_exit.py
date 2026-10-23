# ruff: file-ignore[line-too-long]
"""Epic 31 Celery tasks (AD-9; idempotent, tenant-scoped).

* ``placements_exit.daily_all`` (beat) -> ``placements_exit.daily(tenant_id)`` (queue ``maintenance``): end-date
  alerts, end-of-term cases and a refresh of every open offboarding case.
* ``placements_exit.refresh(tenant_id, placement_id | None, client_id | None)`` (queue ``interactive``): the
  handlers of ``TimesheetApproved``, ``ExpenseClaimDecided``, ``PayrollExportFinalized`` and ``InvoiceIssued``
  re-run the registered verifiers (placements never imports the upper domains, AD-32).
* ``placements_exit.signature_completed(tenant_id, item_id, document_id)``: letter items done on eSign completion.
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids


def _ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, actor_kind="system")


async def _daily(tenant_id: uuid.UUID) -> dict[str, int]:
    from app.services.placements import end_alerts, offboarding

    ctx = _ctx(tenant_id)
    async with tenant_session(tenant_id) as db:
        result = await end_alerts.daily(ctx, db)
    async with tenant_session(tenant_id) as db:
        result["refreshed"] = await offboarding.refresh_all(ctx, db)
    return result


@celery_app.task(name="placements_exit.daily", queue="maintenance", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def daily(tenant_id: str) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _daily(tid), tenant_id=tid))


@celery_app.task(name="placements_exit.daily_all", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def daily_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(daily, str(tid))
    return len(ids)


async def _refresh(tenant_id: uuid.UUID, placement_id: str | None, client_id: str | None) -> int:
    from app.services.placements import offboarding

    ctx = _ctx(tenant_id)
    async with tenant_session(tenant_id) as db:
        if placement_id:
            await offboarding.refresh(ctx, db, uuid.UUID(placement_id))
            return 1
        return await offboarding.refresh_all(ctx, db, client_id=uuid.UUID(client_id) if client_id else None)


@celery_app.task(name="placements_exit.refresh", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def refresh(tenant_id: str, placement_id: str | None = None, client_id: str | None = None) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _refresh(tid, placement_id, client_id), tenant_id=tid))


async def _signature(tenant_id: uuid.UUID, item_id: str, document_id: str) -> Any:
    from app.services.placements import offboarding

    async with tenant_session(tenant_id) as db:
        await offboarding.on_signature_completed(
            _ctx(tenant_id), db, uuid.UUID(item_id), uuid.UUID(document_id)
        )


@celery_app.task(name="placements_exit.signature_completed", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def signature_completed(tenant_id: str, item_id: str, document_id: str) -> None:
    tid = uuid.UUID(str(tenant_id))
    run(lambda: _signature(tid, item_id, document_id), tenant_id=tid)
