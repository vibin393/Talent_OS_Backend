"""Payroll (Epic 28) Celery tasks. Every task starts with ``tenant_id`` (except fan-outs).

* ``payroll.ensure_pay_periods_all`` (beat, daily 01:00 IST) -> ``payroll.ensure_pay_periods(tenant_id)``
  (queue ``finance_runs``): generate pay periods 12 months ahead for every active pay calendar (idempotent).
"""

import uuid

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "finance_runs"


async def _ensure(tenant_id: uuid.UUID) -> int:
    from app.services.payroll import pay_periods

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        return await pay_periods.ensure_pay_periods(ctx, db)


@celery_app.task(name="payroll.ensure_pay_periods", queue="finance_runs", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def ensure_pay_periods(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _ensure(tid), tenant_id=tid))


@celery_app.task(name="payroll.ensure_pay_periods_all", queue="finance_runs")  # type: ignore[misc,untyped-decorator,unused-ignore]
def ensure_pay_periods_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(ensure_pay_periods, str(tid))
    return len(ids)
