"""Sales CRM Celery tasks (Epic 15; queue ``interactive``). Every task starts with ``tenant_id``.

* ``crm.import_leads(tenant_id, batch_id)``: validate + insert a lead CSV batch (enqueued after commit).
* ``crm.mark_contacted(tenant_id, lead_id)``: system New -> Contacted after the first logged contact.
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import run

QUEUE = "interactive"


async def _import(tenant_id: uuid.UUID, batch_id: uuid.UUID) -> dict[str, Any]:
    from app.services.crm import imports

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    try:
        async with tenant_session(tenant_id) as db:
            return await imports.run_import(ctx, db, batch_id)
    except Exception as exc:
        async with tenant_session(tenant_id) as db:
            await imports.fail_import(ctx, db, batch_id, type(exc).__name__)
        raise


@celery_app.task(name="crm.import_leads", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def import_leads(tenant_id: str, batch_id: str) -> dict[str, Any]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _import(tid, uuid.UUID(str(batch_id))), tenant_id=tid))


async def _mark_contacted(tenant_id: uuid.UUID, lead_id: uuid.UUID) -> bool:
    from app.services.crm import leads

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        return await leads.mark_contacted(ctx, db, lead_id)


@celery_app.task(name="crm.mark_contacted", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def mark_contacted(tenant_id: str, lead_id: str) -> bool:
    tid = uuid.UUID(str(tenant_id))
    return bool(run(lambda: _mark_contacted(tid, uuid.UUID(str(lead_id))), tenant_id=tid))
