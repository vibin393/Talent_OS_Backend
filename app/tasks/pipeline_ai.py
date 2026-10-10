"""AI screening summary task (story 6.3, AD-6 / AD-9 / FR-65).

``pipeline_ai.tasks.summarize_ai_screening(tenant_id, screening_id)`` runs with actor ``ai``. By design this
module imports only the summary function: never ``transition_application`` nor the screening outcome
function (``tests/modules/pipeline/test_human_only.py`` checks the import graph).
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.tasks.pipeline import in_tenant, run_sync

QUEUE = "ai"  # Epic 13 (AD-37): the queue of every task in this module


@celery_app.task(name="pipeline_ai.tasks.summarize_ai_screening", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def summarize_ai_screening(tenant_id: str, screening_id: str) -> bool:
    async def _work(ctx: RequestContext, db: AsyncSession) -> bool:
        from app.services.pipeline import ai_summary

        return await ai_summary.summarize(ctx, db, uuid.UUID(str(screening_id)))

    return bool(run_sync(lambda: in_tenant(uuid.UUID(str(tenant_id)), _work, actor="ai")))
