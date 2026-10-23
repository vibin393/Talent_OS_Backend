# ruff: file-ignore[line-too-long]
"""Recruitment marketing Celery tasks (Epic 39). Queue ``campaigns`` (AD-54). Every task starts with ``tenant_id``
(except the beat fan-outs).

* ``campaigns.start_campaign(tenant_id, campaign_id)`` -- ``scheduled -> sending`` + fan-out (``CampaignStartRequested``).
* ``campaigns.send_batch(tenant_id, message_ids)`` -- claim / throttle / govern / deliver <= 500 messages; throttled
  ids re-queue themselves with ``countdown`` = bucket refill + jitter.
* ``campaigns.handle_event(tenant_id, kind, payload)`` -- comms / messaging / pipeline / placement handlers.
* Beats: ``campaigns.tick_all`` (every minute: due campaigns, release deferred, advance sequences, A/B decisions,
  completion), ``campaigns.reputation_all`` (every 15 minutes), ``campaigns.dormant_all`` (hourly; once per
  tenant-local day).
"""

import random
import uuid
from datetime import datetime
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, for_each_tenant, run

QUEUE = "campaigns"


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


# ================================================================ start / send
async def _start(tid: uuid.UUID, cid: uuid.UUID) -> int:
    from app.services.campaigns import campaigns

    async with tenant_session(tid) as db:
        return await campaigns.start_due(db, cid)


@celery_app.task(name="campaigns.start_campaign", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def start_campaign(tenant_id: str, campaign_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _start(tid, uuid.UUID(str(campaign_id))), tenant_id=tid) or 0)


async def _send(tid: uuid.UUID, ids: list[uuid.UUID]) -> dict[str, Any]:
    from app.services.campaigns import sending

    async with tenant_session(tid) as db:
        result = await sending.send_batch(db, ids)
    return {
        "sent": result.sent,
        "deferred": result.deferred,
        "suppressed": result.suppressed,
        "skipped": result.skipped,
        "failed": result.failed,
        "throttled": [str(i) for i in result.throttled],
        "retry_after_ms": result.retry_after_ms,
    }


@celery_app.task(name="campaigns.send_batch", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def send_batch(tenant_id: str, message_ids: list[str]) -> dict[str, Any]:
    tid = uuid.UUID(str(tenant_id))
    ids = [i for i in (_uuid(m) for m in message_ids) if i is not None]
    out = dict(run(lambda: _send(tid, ids), tenant_id=tid) or {})
    throttled = out.get("throttled") or []
    if throttled:
        countdown = max(1, int(out.get("retry_after_ms", 1000)) // 1000 + 1) + random.randint(0, 3)  # nosec B311 - non-cryptographic sampling/jitter
        if celery_app.conf.task_always_eager:  # tests / eager: the beat releases them instead of recursing
            return out
        try:
            send_batch.apply_async((str(tid), throttled), countdown=countdown)
        except Exception:
            enqueue(send_batch, str(tid), throttled)
    return out


# ================================================================ events
async def _handle(tid: uuid.UUID, kind: str, payload: dict[str, Any]) -> Any:
    from app.services.campaigns import attribution, engagement, triggers

    async with tenant_session(tid) as db:
        if kind == "email_sent":
            return await engagement.on_email_sent(db, uuid.UUID(payload["message_id"]))
        if kind == "email_failed":
            return await engagement.on_email_failed(db, uuid.UUID(payload["message_id"]))
        if kind == "email_bounced":
            return await engagement.on_email_bounced(
                db, uuid.UUID(payload["message_id"]), str(payload.get("bounce_type"))
            )
        if kind == "channel_status":
            return await engagement.on_channel_status(
                db, uuid.UUID(payload["message_id"]), str(payload.get("status"))
            )
        if kind == "opt_out":
            return await engagement.on_opt_out_recorded(
                db, _uuid(payload.get("candidate_id")), str(payload.get("channel"))
            )
        if kind == "reply":
            return await engagement.on_reply(
                db, _uuid(payload.get("candidate_id")), payload.get("channel"), str(payload.get("ref") or "")
            )
        if kind == "application_created":
            return await attribution.on_application_created(db, uuid.UUID(payload["application_id"]))
        if kind == "hired":
            cid = _uuid(payload.get("candidate_id"))
            return await attribution.on_hired(db, cid, _uuid(payload.get("application_id"))) if cid else False
        if kind == "rediscovery":
            return await triggers.silver_medalists_matched(db, tid, uuid.UUID(payload["demand_id"]))
    return None


@celery_app.task(name="campaigns.handle_event", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def handle_event(tenant_id: str, kind: str, payload: dict[str, Any]) -> Any:
    tid = uuid.UUID(str(tenant_id))
    result = run(lambda: _handle(tid, kind, dict(payload or {})), tenant_id=tid)
    return result if isinstance(result, (bool, int, type(None))) else str(result)


# ================================================================ beats
async def _release_deferred(db: Any, at: datetime) -> int:
    from sqlalchemy import select

    from app.models.campaigns import CampaignMessage
    from app.services.campaigns import sending

    ids = list(
        (
            await db.execute(
                select(CampaignMessage.id)
                .where(
                    ((CampaignMessage.status == "pending") & CampaignMessage.defer_until.is_(None))
                    | ((CampaignMessage.status == "deferred") & (CampaignMessage.defer_until <= at)),
                    CampaignMessage.campaign_id.is_not(None),
                )
                .limit(2000)
            )
        ).scalars()
    )
    if not ids:
        return 0
    await sending.send_batch(db, ids, now=at)
    return len(ids)


async def tick(tid: uuid.UUID, at: datetime | None = None) -> dict[str, int]:
    """One minute of marketing work for a tenant (also called directly by tests with a fixed ``at``)."""
    from app.services.campaigns import campaigns, sequences
    from app.services.campaigns.common import now

    when = at or now()
    out: dict[str, int] = {}
    async with tenant_session(tid) as db:
        out["started"] = await campaigns.start_due(db, at=when)
    async with tenant_session(tid) as db:
        out["released"] = await _release_deferred(db, when)
    async with tenant_session(tid) as db:
        out["advanced"] = await sequences.advance(db, at=when)
    async with tenant_session(tid) as db:
        out["ab_decided"] = await campaigns.decide_ab(db, at=when)
    async with tenant_session(tid) as db:
        out["completed"] = await campaigns.complete_finished(db)
    return out


@celery_app.task(name="campaigns.tick", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def tick_tenant(tenant_id: str) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: tick(tid), tenant_id=tid) or {})


@celery_app.task(name="campaigns.tick_all", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def tick_all() -> dict[str, Any]:
    async def _all() -> dict[str, Any]:
        return await for_each_tenant(lambda tid: _enqueue(tick_tenant, tid))

    return dict(run(_all) or {})


async def _enqueue(task: Any, tid: uuid.UUID) -> str:
    enqueue(task, str(tid))
    return "queued"


async def _reputation(tid: uuid.UUID) -> str | None:
    from app.services.campaigns import reputation

    async with tenant_session(tid) as db:
        return await reputation.check(db)


@celery_app.task(name="campaigns.reputation", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def reputation_tenant(tenant_id: str) -> str | None:
    tid = uuid.UUID(str(tenant_id))
    return run(lambda: _reputation(tid), tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="campaigns.reputation_all", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def reputation_all() -> dict[str, Any]:
    async def _all() -> dict[str, Any]:
        return await for_each_tenant(lambda tid: _enqueue(reputation_tenant, tid))

    return dict(run(_all) or {})


async def _dormant(tid: uuid.UUID) -> int:
    from app.services.campaigns import triggers

    async with tenant_session(tid) as db:
        return await triggers.detect_dormant(db, tid)


@celery_app.task(name="campaigns.dormant", queue=QUEUE, acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def dormant_tenant(tenant_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _dormant(tid), tenant_id=tid) or 0)


@celery_app.task(name="campaigns.dormant_all", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def dormant_all() -> dict[str, Any]:
    async def _all() -> dict[str, Any]:
        return await for_each_tenant(lambda tid: _enqueue(dormant_tenant, tid))

    return dict(run(_all) or {})
