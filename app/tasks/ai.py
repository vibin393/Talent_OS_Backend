"""AI embedding Celery tasks (Epic 5, story 5.2; AD-9: ``tenant_id`` first, idempotent).

* ``ai.tasks.embed_candidates(tenant_id, candidate_ids)`` -- (re-)embed; skips unchanged ``content_hash`` and
  candidates without AI consent (their vectors are deleted).
* ``ai.tasks.embed_dirty(tenant_id)`` -- drains the debounced Redis set ``emb:dirty:<tenant>``.
* ``ai.tasks.embed_demand(tenant_id, demand_id)``; ``ai.tasks.delete_candidate_embeddings(tenant_id, ids)``;
  ``ai.tasks.backfill_embeddings(tenant_id)``.
"""

import uuid
from typing import Any

from app.core import dirty_sets
from app.core.celery import celery_app
from app.tasks.epic5_runtime import in_tenant_session, run_sync, uuids

QUEUE = "ai"  # Epic 13 (AD-37): the queue of every task in this module

DIRTY_PREFIX = "emb:dirty:"


def dirty_key(tenant_id: uuid.UUID | str) -> str:
    return f"{DIRTY_PREFIX}{tenant_id}"


async def embed_candidates_async(tenant_id: uuid.UUID, candidate_ids: list[uuid.UUID]) -> int:
    from app.services import embeddings_service

    async def work(_ctx: Any, db: Any) -> int:
        return len(await embeddings_service.sync_candidates(db, tenant_id, candidate_ids))

    return int(await in_tenant_session(tenant_id, work))


async def delete_candidate_embeddings_async(tenant_id: uuid.UUID, candidate_ids: list[uuid.UUID]) -> int:
    from app.services import embeddings_service

    async def work(_ctx: Any, db: Any) -> int:
        return await embeddings_service.delete_candidates(db, tenant_id, candidate_ids)

    return int(await in_tenant_session(tenant_id, work))


async def embed_demand_async(tenant_id: uuid.UUID, demand_id: uuid.UUID) -> bool:
    from app.services import embeddings_service

    async def work(_ctx: Any, db: Any) -> bool:
        return await embeddings_service.sync_demand(db, tenant_id, demand_id) is not None

    return bool(await in_tenant_session(tenant_id, work))


async def backfill_async(tenant_id: uuid.UUID) -> int:
    from app.services import embeddings_service

    async def work(_ctx: Any, db: Any) -> int:
        return await embeddings_service.backfill(db, tenant_id)

    return int(await in_tenant_session(tenant_id, work))


@celery_app.task(name="ai.tasks.embed_candidates", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def embed_candidates(tenant_id: str, candidate_ids: list[str]) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run_sync(lambda: embed_candidates_async(tid, uuids(candidate_ids))))


@celery_app.task(name="ai.tasks.embed_dirty", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def embed_dirty(tenant_id: str) -> int:
    ids = uuids(dirty_sets.drain(dirty_key(tenant_id)))
    if not ids:
        return 0
    tid = uuid.UUID(str(tenant_id))
    return int(run_sync(lambda: embed_candidates_async(tid, ids)))


@celery_app.task(name="ai.tasks.delete_candidate_embeddings", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def delete_candidate_embeddings(tenant_id: str, candidate_ids: list[str]) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run_sync(lambda: delete_candidate_embeddings_async(tid, uuids(candidate_ids))))


@celery_app.task(name="ai.tasks.embed_demand", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def embed_demand(tenant_id: str, demand_id: str) -> bool:
    tid, did = uuid.UUID(str(tenant_id)), uuid.UUID(str(demand_id))
    return bool(run_sync(lambda: embed_demand_async(tid, did)))


@celery_app.task(name="ai.tasks.backfill_embeddings", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def backfill_embeddings(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run_sync(lambda: backfill_async(tid)))
