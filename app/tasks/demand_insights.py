# ruff: noqa: E501
"""Demand insights Celery tasks (Epic 55, A7): ``demand_insights.expire_flags`` (beat, daily, queue ``maintenance``) --
duplicate flags on demands closed > 90 days ago expire; SLA flags on met / closed clocks resolve."""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "maintenance"


@celery_app.task(name="demand_insights.expire_flags", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def expire_flags() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> int:
        from app.services import demand_insights_service as svc

        async with tenant_session(tid) as db:
            return await svc.expire_flags(RequestContext(tenant_id=tid, actor_kind="system"), db)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


__all__ = ["expire_flags"]
