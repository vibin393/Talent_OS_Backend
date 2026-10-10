"""Placements Celery tasks (Epic 19). Every task starts with ``tenant_id`` (except fan-outs).

* ``placements.create_from_offer(tenant_id, offer_id)`` (queue ``interactive``): the ``OfferAccepted`` handler;
  idempotent (one Placement per offer).
* ``placements.upstream(tenant_id, event_name, payload)`` (queue ``interactive``): gate rows / caches from
  BGV, onboarding, compliance and commercials events (AD-25).
* ``placements.daily_all`` (beat, daily 02:15 IST) -> ``placements.daily(tenant_id)`` (queue ``maintenance``):
  touchpoints (+ joining-confirmation emails sent after commit), unmet-gate alerts, dropout rules.
"""

import uuid
from typing import Any

from loguru import logger

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.mailer import send_email
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "maintenance"


async def _create_from_offer(tenant_id: uuid.UUID, offer_id: uuid.UUID) -> str | None:
    from app.services.placements import creation

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    try:
        async with tenant_session(tenant_id) as db:
            p = await creation.create_from_offer(ctx, db, offer_id)
            return p.human_id
    except Exception:
        logger.exception("could not create the placement of offer {}", offer_id)
        return None


@celery_app.task(name="placements.create_from_offer", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def create_from_offer(tenant_id: str, offer_id: str) -> str | None:
    tid = uuid.UUID(str(tenant_id))
    result: str | None = run(lambda: _create_from_offer(tid, uuid.UUID(str(offer_id))), tenant_id=tid)
    return result


async def _upstream(tenant_id: uuid.UUID, name: str, payload: dict[str, Any]) -> int:
    from app.services.placements import gates

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        return await gates.handle_upstream(ctx, db, name, payload)


@celery_app.task(name="placements.upstream", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def upstream(tenant_id: str, event_name: str, payload: dict[str, Any]) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _upstream(tid, event_name, payload), tenant_id=tid))


async def _daily(tenant_id: uuid.UUID) -> dict[str, int]:
    from app.services.placements import prejoining

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        result = await prejoining.run_daily(ctx, db)
    for message in result.emails:  # after commit: the touchpoint rows and links exist
        try:
            await send_email(message)
        except Exception:
            logger.exception("joining-confirmation email failed")
    return {"touchpoints": result.touchpoints, "alerts": result.alerts, "raised": result.raised}


@celery_app.task(name="placements.daily", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def daily(tenant_id: str) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _daily(tid), tenant_id=tid))


@celery_app.task(name="placements.daily_all")  # type: ignore[misc,untyped-decorator,unused-ignore]
def daily_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(daily, str(tid))
    return len(ids)


# ---------------------------------------------------------------- Epic 24 (bench management)
async def _bench_event(tenant_id: uuid.UUID, kind: str, payload: dict[str, Any]) -> str | None:
    """Idempotent bench reactions (``services/placements/bench*.py``); a partial-unique race is a no-op."""
    from sqlalchemy.exc import IntegrityError

    from app.services.placements import bench, bench_matching

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    try:
        async with tenant_session(tenant_id) as db:
            if kind == "placement_ended":
                result = await bench.on_placement_ended(ctx, db, payload)
                return str(result) if result else None
            if kind == "placement_activated":
                return str(await bench.on_placement_activated(ctx, db, payload))
            if kind == "submission_created":
                return str(await bench.on_submission_created(ctx, db, uuid.UUID(payload["application_id"])))
            if kind == "application_closed":
                return str(
                    await bench.on_application_closed(
                        ctx, db, uuid.UUID(payload["application_id"]), uuid.UUID(payload["candidate_id"])
                    )
                )
            if kind == "match_run":
                return str(
                    await bench_matching.notify_strong(ctx, db, demand_ids=[uuid.UUID(payload["demand_id"])])
                )
            if kind == "entry_created":
                return str(
                    await bench_matching.notify_strong(
                        ctx, db, candidate_ids=[uuid.UUID(payload["candidate_id"])]
                    )
                )
    except IntegrityError:
        logger.info("bench event {} raced with a concurrent delivery (no-op)", kind)
    return None


@celery_app.task(name="placements.bench_event", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def bench_event(tenant_id: str, kind: str, payload: dict[str, Any]) -> str | None:
    tid = uuid.UUID(str(tenant_id))
    result: str | None = run(lambda: _bench_event(tid, kind, payload), tenant_id=tid)
    return result
