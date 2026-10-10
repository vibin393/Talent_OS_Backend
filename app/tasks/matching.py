"""Matching Celery tasks (Epic 5, stories 5.4/5.5; AD-9: ``tenant_id`` first, idempotent).

* ``matching.tasks.start_run(tenant_id, demand_id, trigger)`` -- commits a ``running`` run row (visible to
  the UI) and hands over to ``run_for_demand`` (via ``MatchRunRequested``).
* ``matching.tasks.run_for_demand(tenant_id, demand_id, run_id)`` -- the scoring run; a failing run is
  retried 3 times, then marked ``failed``.
* ``matching.tasks.start_dirty_runs(tenant_id)`` / ``score_dirty(tenant_id)`` -- drain the debounced Redis
  sets ``match:dirty:demand:<tenant>`` / ``match:dirty:cand:<tenant>``.
* ``matching.tasks.score_candidates(tenant_id, ids)``, ``delete_candidate_matches``, ``mark_stale``,
  ``explain_matches``, ``rediscovery_timeline``.
* ``matching.tasks.sweep_dirty`` (beat, every 2 min) -- safety net draining every dirty set.
"""

import time
import uuid
from typing import Any

from loguru import logger

from app.core import dirty_sets
from app.core.celery import celery_app
from app.tasks.epic5_runtime import in_tenant_session, is_eager, run_sync, uuids

QUEUE = "interactive"  # Epic 13 (AD-37): the queue of every task in this module

RUN_ATTEMPTS = 4  # first try + 3 retries
DEMAND_DIRTY = "match:dirty:demand:"
CAND_DIRTY = "match:dirty:cand:"


def demand_dirty_key(tenant_id: uuid.UUID | str) -> str:
    return f"{DEMAND_DIRTY}{tenant_id}"


def cand_dirty_key(tenant_id: uuid.UUID | str) -> str:
    return f"{CAND_DIRTY}{tenant_id}"


# ================================================================ async bodies
async def start_run_async(tenant_id: uuid.UUID, demand_id: uuid.UUID, trigger: str) -> uuid.UUID | None:
    from app.services.demands import matching_port as demands
    from app.services.matching import runs

    async def work(_ctx: Any, db: Any) -> uuid.UUID | None:
        if await demands.demand_status(db, demand_id) != "open":
            return None
        return (await runs.request_run(db, tenant_id, demand_id, trigger)).id

    return await in_tenant_session(tenant_id, work)  # type: ignore[no-any-return]


async def run_for_demand_async(tenant_id: uuid.UUID, run_id: uuid.UUID) -> str:
    from app.services.matching import runs

    async def work(ctx: Any, db: Any) -> str:
        run = await runs.execute_run(ctx, db, run_id)
        return run.status if run is not None else "missing"

    return str(await in_tenant_session(tenant_id, work))


async def fail_run_async(tenant_id: uuid.UUID, run_id: uuid.UUID, error: str) -> None:
    from app.services.matching import runs

    async def work(_ctx: Any, db: Any) -> None:
        await runs.fail_run(db, run_id, error)

    await in_tenant_session(tenant_id, work)


async def score_candidates_async(tenant_id: uuid.UUID, candidate_ids: list[uuid.UUID]) -> int:
    from app.services.matching import runs

    async def work(ctx: Any, db: Any) -> int:
        total = 0
        for cid in dict.fromkeys(candidate_ids):
            total += await runs.score_candidate_against_open_demands(ctx, db, tenant_id, cid)
        return total

    return int(await in_tenant_session(tenant_id, work))


async def refresh_candidates_async(tenant_id: uuid.UUID, candidate_ids: list[uuid.UUID]) -> int:
    """Embeddings then scores for ``candidate_ids`` in one session (the eager-mode path of the handlers)."""
    from app.services import embeddings_service
    from app.services.matching import runs

    async def work(ctx: Any, db: Any) -> int:
        await embeddings_service.sync_candidates(db, tenant_id, candidate_ids)
        total = 0
        for cid in dict.fromkeys(candidate_ids):
            total += await runs.score_candidate_against_open_demands(ctx, db, tenant_id, cid)
        return total

    return int(await in_tenant_session(tenant_id, work))


async def delete_candidate_matches_async(tenant_id: uuid.UUID, candidate_ids: list[uuid.UUID]) -> int:
    from app.services.matching import runs

    async def work(_ctx: Any, db: Any) -> int:
        return await runs.delete_candidate_matches(db, candidate_ids)

    return int(await in_tenant_session(tenant_id, work))


async def mark_stale_async(tenant_id: uuid.UUID, demand_id: uuid.UUID) -> int:
    from app.services.matching import runs

    async def work(_ctx: Any, db: Any) -> int:
        return await runs.mark_stale(db, demand_id)

    return int(await in_tenant_session(tenant_id, work))


async def explain_matches_async(tenant_id: uuid.UUID, demand_id: uuid.UUID) -> dict[str, int]:
    from app.services.matching import explain

    async def work(ctx: Any, db: Any) -> dict[str, int]:
        return await explain.explain_matches(ctx, db, demand_id)

    return await in_tenant_session(tenant_id, work)  # type: ignore[no-any-return]


async def rediscovery_timeline_async(
    tenant_id: uuid.UUID, demand_id: uuid.UUID, funnel: dict[str, Any], assignee_ids: list[uuid.UUID]
) -> None:
    from app.services import activity_service

    async def work(ctx: Any, db: Any) -> None:
        steps = [funnel.get(k, 0) for k in ("total", "relevant", "strong", "available")]
        body = "Rediscovery: " + " → ".join(f"{int(n):,}" for n in steps)
        await activity_service.add_system_entry(
            ctx, db, "demand", demand_id, body, {"funnel": funnel, "kind": "rediscovery"}
        )

    await in_tenant_session(tenant_id, work)
    for user_id in assignee_ids:
        # Notification intent (Epic 10 delivers it from RediscoveryCompleted). Ids only, no PII.
        logger.info(
            "Notification intent: rediscovery demand={} user={} funnel={}", demand_id, user_id, funnel
        )


# ================================================================ tasks
@celery_app.task(name="matching.tasks.start_run", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def start_run(tenant_id: str, demand_id: str, trigger: str) -> str | None:
    tid, did = uuid.UUID(str(tenant_id)), uuid.UUID(str(demand_id))
    run_id = run_sync(lambda: start_run_async(tid, did, trigger))
    return str(run_id) if run_id is not None else None


@celery_app.task(name="matching.tasks.run_for_demand", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def run_for_demand(tenant_id: str, demand_id: str, run_id: str) -> str:  # noqa: ARG001 - demand id kept for tracing
    tid, rid = uuid.UUID(str(tenant_id)), uuid.UUID(str(run_id))
    last_error: BaseException | None = None
    for attempt in range(1, RUN_ATTEMPTS + 1):
        try:
            return str(run_sync(lambda: run_for_demand_async(tid, rid)))
        except Exception as exc:
            last_error = exc
            logger.exception("Match run {} failed (attempt {}/{})", rid, attempt, RUN_ATTEMPTS)
            if attempt < RUN_ATTEMPTS and not is_eager():
                time.sleep(min(30, 2**attempt))
    run_sync(lambda: fail_run_async(tid, rid, type(last_error).__name__))
    return "failed"


@celery_app.task(name="matching.tasks.start_dirty_runs", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def start_dirty_runs(tenant_id: str) -> int:
    ids = uuids(dirty_sets.drain(demand_dirty_key(tenant_id)))
    tid = uuid.UUID(str(tenant_id))
    for did in ids:
        run_sync(lambda did=did: start_run_async(tid, did, "requirements_changed"))
    return len(ids)


@celery_app.task(name="matching.tasks.score_candidates", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def score_candidates(tenant_id: str, candidate_ids: list[str]) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run_sync(lambda: score_candidates_async(tid, uuids(candidate_ids))))


@celery_app.task(name="matching.tasks.score_dirty", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def score_dirty(tenant_id: str) -> int:
    ids = uuids(dirty_sets.drain(cand_dirty_key(tenant_id)))
    if not ids:
        return 0
    tid = uuid.UUID(str(tenant_id))
    return int(run_sync(lambda: score_candidates_async(tid, ids)))


@celery_app.task(name="matching.tasks.refresh_candidates", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def refresh_candidates(tenant_id: str, candidate_ids: list[str]) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run_sync(lambda: refresh_candidates_async(tid, uuids(candidate_ids))))


@celery_app.task(name="matching.tasks.delete_candidate_matches", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def delete_candidate_matches(tenant_id: str, candidate_ids: list[str]) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run_sync(lambda: delete_candidate_matches_async(tid, uuids(candidate_ids))))


@celery_app.task(name="matching.tasks.mark_stale", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def mark_stale(tenant_id: str, demand_id: str) -> int:
    tid, did = uuid.UUID(str(tenant_id)), uuid.UUID(str(demand_id))
    return int(run_sync(lambda: mark_stale_async(tid, did)))


@celery_app.task(name="matching.tasks.explain_matches", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def explain_matches(tenant_id: str, demand_id: str) -> dict[str, int]:
    tid, did = uuid.UUID(str(tenant_id)), uuid.UUID(str(demand_id))
    return run_sync(lambda: explain_matches_async(tid, did))  # type: ignore[no-any-return]


@celery_app.task(name="matching.tasks.rediscovery_timeline", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def rediscovery_timeline(
    tenant_id: str, demand_id: str, funnel: dict[str, Any], assignee_ids: list[str]
) -> None:
    tid, did = uuid.UUID(str(tenant_id)), uuid.UUID(str(demand_id))
    run_sync(lambda: rediscovery_timeline_async(tid, did, funnel, uuids(assignee_ids)))


@celery_app.task(name="matching.tasks.sweep_dirty")  # type: ignore[misc,untyped-decorator,unused-ignore]
def sweep_dirty() -> int:
    """Beat safety net: drain every debounced dirty set (embeddings, candidate scores, demand runs)."""
    from app.tasks import ai as ai_tasks

    enqueued = 0
    for key in dirty_sets.keys(f"{ai_tasks.DIRTY_PREFIX}*"):
        ai_tasks.embed_dirty.delay(key.removeprefix(ai_tasks.DIRTY_PREFIX))
        enqueued += 1
    for key in dirty_sets.keys(f"{CAND_DIRTY}*"):
        score_dirty.delay(key.removeprefix(CAND_DIRTY))
        enqueued += 1
    for key in dirty_sets.keys(f"{DEMAND_DIRTY}*"):
        start_dirty_runs.delay(key.removeprefix(DEMAND_DIRTY))
        enqueued += 1
    return enqueued
