# ruff: file-ignore[line-too-long]
"""Analytics answers maintenance (Epic 64, A8). Beat (queue ``maintenance``): ``analytics_answers.purge_expired`` daily --
deletes answers past ``expires_at`` (90 days) and answers whose Copilot conversation no longer exists."""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "maintenance"


@celery_app.task(name="analytics_answers.purge_expired", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_expired() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> int:
        from app.services import analytics_answers_service

        async with tenant_session(tid) as db:
            return await analytics_answers_service.purge_expired(db)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]
