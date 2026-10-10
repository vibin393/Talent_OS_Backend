"""Search Celery tasks (Epic 5, AD-9; ``tenant_id`` first, idempotent).

* ``search.tasks.refresh_docs(tenant_id, candidate_ids)`` -- upsert the candidates' search projection rows.
* ``search.tasks.reindex(tenant_id)`` -- rebuild the tenant's projection in batches of 1,000.
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.tasks.epic5_runtime import in_tenant_session, run_sync, uuids

QUEUE = "interactive"  # Epic 13 (AD-37): the queue of every task in this module


async def refresh_docs_async(tenant_id: uuid.UUID, candidate_ids: list[uuid.UUID]) -> dict[str, int]:
    from app.services.search import projection

    async def work(_ctx: Any, db: Any) -> dict[str, int]:
        return await projection.refresh_docs(db, tenant_id, candidate_ids)

    return await in_tenant_session(tenant_id, work)  # type: ignore[no-any-return]


async def reindex_async(tenant_id: uuid.UUID) -> int:
    from app.services.search import projection

    async def work(_ctx: Any, db: Any) -> int:
        return await projection.reindex(db, tenant_id)

    return int(await in_tenant_session(tenant_id, work))


@celery_app.task(name="search.tasks.refresh_docs", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def refresh_docs(tenant_id: str, candidate_ids: list[str]) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))
    return run_sync(lambda: refresh_docs_async(tid, uuids(candidate_ids)))  # type: ignore[no-any-return]


@celery_app.task(name="search.tasks.reindex", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def reindex(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run_sync(lambda: reindex_async(tid)))
