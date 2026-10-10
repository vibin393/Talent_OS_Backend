# ruff: noqa: E501
"""Delivery scheduling (Epic 68, §0.5/§0.7, AD-79, NFR-66).

* ``pump(tenant_id)`` -- serialised per tenant (transaction advisory lock): due rows (``pending|retrying`` and
  ``next_attempt_at <= now``, ``next_attempt_at, event_seq`` order, 200 per batch, ``FOR UPDATE SKIP LOCKED``) become
  ``in_flight`` while the Subscription has a free slot (``max_in_flight``, counted in the DB) and the tenant is under
  its tier cap (``webhook_tenant_concurrency``); suspended owners are pushed back 60 s without counting an attempt.
  ``webhooks.deliver`` is enqueued for each row after commit.
* ``deliver(tenant_id, delivery_id)`` -- one attempt: body (outbox envelope / stored body), signature, POST, then the
  ladder (1 + 8 retries, D-24), 4xx / 410 / 429 / egress rules, health, metering.
* ``pump_all`` -- the 5 s beat: every active tenant with due rows (fairness: each tenant's pump enqueues at most its
  own cap, so one tenant's failing endpoints never occupy the shared worker pool).
"""

import random
import uuid
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.settings import get_settings
from app.core.tenancy import is_sandbox
from app.events.webhooks import WebhookDeliveryFailed
from app.models.webhooks import WebhookDelivery, WebhookSubscription
from app.services.webhooks import common, delivery, signing
from app.services.webhooks.subscriptions import disable, paused

BATCH = 200
PAUSE_SECONDS = 60
STUCK_SECONDS = 60
MAX_ATTEMPT_LOG = 9


def max_attempts() -> int:
    return 1 + len(get_settings().webhook_retry_delays_seconds)


def jittered(seconds: int, *, rng: random.Random | None = None) -> float:
    pct = get_settings().webhook_retry_jitter_pct / 100.0
    r = (rng or random).uniform(-pct, pct)
    return max(1.0, seconds * (1 + r))


def plan_next(attempt: int, status_code: int | None, error: str | None, retry_after: int | None) -> tuple[str, float | None]:  # noqa: ARG001
    """After ``attempt`` (1-based) failed: ``("retrying", delay_seconds)`` or ``("failed", None)``."""
    s = get_settings()
    delays = list(s.webhook_retry_delays_seconds)
    if status_code == 410:
        return "failed", None
    if status_code is not None and 400 <= status_code < 500 and status_code not in (408, 409, 429):
        if attempt <= s.webhook_client_error_retries and attempt <= len(delays):
            return "retrying", jittered(delays[attempt - 1])
        return "failed", None
    if attempt >= max_attempts():
        return "failed", None
    if status_code == 429 and retry_after is not None:
        return "retrying", float(min(max(retry_after, 1), s.webhook_retry_after_cap_seconds))
    return "retrying", jittered(delays[attempt - 1])


# ------------------------------------------------------------------ suspension (kill switch / App / Installation)
async def _paused_map(ctx: RequestContext, db: AsyncSession, subs: dict[uuid.UUID, WebhookSubscription]) -> dict[uuid.UUID, bool]:
    return {sid: await paused(ctx, db, sub) for sid, sub in subs.items()}


async def _tier_cap(tenant_id: uuid.UUID) -> int:
    s = get_settings()
    caps = s.webhook_tenant_concurrency
    try:
        from app.services import metering_service

        name = (await metering_service.resolve_tier(tenant_id)).name
    except Exception:
        name = "standard"
    if await is_sandbox(tenant_id):
        name = "sandbox"
    return int(caps.get(name, caps.get("standard", 16)))


async def pump(tenant_id: uuid.UUID) -> list[uuid.UUID]:
    """Mark due rows ``in_flight`` (slots permitting) and return their ids; the caller enqueues the deliveries."""
    tid = uuid.UUID(str(tenant_id))
    ctx = RequestContext(tenant_id=tid, actor_kind="system")
    cap = await _tier_cap(tid)
    now = common.now()
    picked: list[uuid.UUID] = []
    async with tenant_session(tid) as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"webhooks.pump:{tid}"})
        inflight = dict(
            (await db.execute(select(WebhookDelivery.subscription_id, func.count()).where(WebhookDelivery.status == "in_flight").group_by(WebhookDelivery.subscription_id))).all()
        )
        total = sum(inflight.values())
        if total >= cap:
            return []
        due = list(
            (
                await db.execute(
                    select(WebhookDelivery)
                    .where(WebhookDelivery.status.in_(("pending", "retrying")), WebhookDelivery.next_attempt_at <= now)
                    .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.event_seq.nulls_last())
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        if not due:
            return []
        subs = {
            s.id: s
            for s in (await db.execute(select(WebhookSubscription).where(WebhookSubscription.id.in_({d.subscription_id for d in due})))).scalars()
        }
        pmap = await _paused_map(ctx, db, subs)
        for d in due:
            sub = subs.get(d.subscription_id)
            if sub is None or sub.deleted_at is not None:
                d.status, d.next_attempt_at = "cancelled", None
                continue
            if sub.status == "disabled":
                d.status = "held"
                continue
            if sub.status != "active" and d.kind not in ("test", "verification"):
                continue
            if pmap.get(sub.id):
                d.next_attempt_at = now + timedelta(seconds=PAUSE_SECONDS)  # paused: not an attempt
                continue
            if total >= cap or inflight.get(sub.id, 0) >= sub.max_in_flight:
                continue
            d.status, d.updated_at = "in_flight", now
            inflight[sub.id] = inflight.get(sub.id, 0) + 1
            total += 1
            picked.append(d.id)
    return picked


# ------------------------------------------------------------------ one attempt
async def _body(ctx: RequestContext, db: AsyncSession, d: WebhookDelivery) -> tuple[bytes | None, dict[str, Any] | None]:
    if d.body is not None:
        return bytes(d.body), None
    from app.services import public_events_service
    from app.services.public_events.outbox import PublicEventRow

    try:
        ev = await public_events_service.get_event(RequestContext(tenant_id=ctx.tenant_id, actor_kind="system"), db, d.event_id)
    except Exception:
        return None, None
    row = PublicEventRow(
        id=ev.id, seq=ev.sequence, tenant_id=ev.tenant_ref.id, event_type=ev.type, event_version=ev.version, visibility="public",
        subject_type=ev.subject.type, subject_id=ev.subject.id, occurred_at=ev.occurred_at, data=ev.data, actor_kind=None,
        app_installation_id=None,
    )  # fmt: skip
    return public_events_service.envelope(row, ev.tenant_ref.slug, ev.livemode), None


async def _health_success(sub: WebhookSubscription) -> None:
    sub.consecutive_failures, sub.failure_streak_started_at, sub.streak_notified_at = 0, None, None
    sub.last_success_at = common.now()


def _health_failure(db: AsyncSession, sub: WebhookSubscription, d: WebhookDelivery, attempt: int, final: bool) -> None:
    if d.kind == "test":  # test events never start or extend a failure streak
        return
    first = sub.consecutive_failures == 0
    sub.consecutive_failures = (sub.consecutive_failures or 0) + 1
    sub.last_failure_at = common.now()
    if sub.failure_streak_started_at is None:
        sub.failure_streak_started_at = common.now()
    if first or final:
        events.emit(
            db,
            WebhookDeliveryFailed(tenant_id=sub.tenant_id, subscription_id=sub.id, delivery_id=d.id, event_type=d.event_type, attempt=attempt, final=final),
        )


async def attempt(ctx: RequestContext, db: AsyncSession, d: WebhookDelivery, sub: WebhookSubscription) -> delivery.Outcome:
    """Send ``d`` once and record the outcome on ``d`` / ``sub`` (in ``db``; the caller commits)."""
    n = int(d.attempt_count or 0) + 1
    body, _ = await _body(ctx, db, d)
    now = common.now()
    if body is None:
        outcome = delivery.Outcome(None, 0, error="event_expired")
    else:
        keys = await signing.signing_secrets(ctx, db, sub.id)
        if not keys:
            outcome = delivery.Outcome(None, 0, error="no_secret")
        else:
            hdrs = signing.headers(
                keys, body, event_id=d.event_id, event_type=d.event_type, delivery_id=d.id, attempt=n,
                replay=d.kind == "replay", test=d.kind == "test",
            )  # fmt: skip
            outcome = await delivery.post(db, sub.tenant_id, sub.url, body, hdrs, sandbox=await is_sandbox(sub.tenant_id))
    log = list(d.attempts or [])
    log.append({"n": n, "at": now.isoformat(), "status_code": outcome.status_code, "latency_ms": outcome.latency_ms, "error": outcome.error, "excerpt": outcome.excerpt})
    d.attempts = log[-MAX_ATTEMPT_LOG:]
    d.attempt_count = n
    d.last_status_code = outcome.status_code
    d.last_error = (outcome.error or None) and outcome.error[:60]  # type: ignore[index]
    d.updated_at = now
    if outcome.ok:
        d.status, d.next_attempt_at, d.delivered_at = "succeeded", None, now
        if d.kind != "test":
            await _health_success(sub)
    elif outcome.security_refusal or outcome.error in ("event_expired", "no_secret"):
        d.status, d.next_attempt_at = "failed", None
        _health_failure(db, sub, d, n, True)
        if outcome.security_refusal and d.kind != "test":
            await disable(db, sub, "egress_refused")
    else:
        status, delay = plan_next(n, outcome.status_code, outcome.error, outcome.retry_after)
        d.status = status
        d.next_attempt_at = now + timedelta(seconds=delay) if delay is not None else None
        _health_failure(db, sub, d, n, status == "failed")
        if outcome.status_code == 410 and d.kind != "test":
            await disable(db, sub, "gone")
    from app.services import metering_service

    await metering_service.record(ctx, "webhook_attempt", installation_id=sub.installation_id, route_group=d.event_type[:60], error=not outcome.ok)
    return outcome


async def deliver(tenant_id: uuid.UUID, delivery_id: uuid.UUID) -> str | None:
    """Task body: one attempt of an ``in_flight`` row (idempotent: any other status is left alone)."""
    tid = uuid.UUID(str(tenant_id))
    ctx = RequestContext(tenant_id=tid, actor_kind="system")
    again = False
    async with tenant_session(tid) as db:
        d = (await db.execute(select(WebhookDelivery).where(WebhookDelivery.id == delivery_id).with_for_update(skip_locked=True))).scalar_one_or_none()
        if d is None or d.status != "in_flight":
            return None
        sub = (await db.execute(select(WebhookSubscription).where(WebhookSubscription.id == d.subscription_id).with_for_update())).scalar_one_or_none()
        if sub is None or sub.deleted_at is not None:
            d.status, d.next_attempt_at = "cancelled", None
            return d.status
        if sub.status == "disabled":
            d.status = "held"
            return d.status
        if await paused(ctx, db, sub):
            d.status = "retrying" if d.attempt_count else "pending"
            d.next_attempt_at = common.now() + timedelta(seconds=PAUSE_SECONDS)
            return d.status
        await attempt(ctx, db, d, sub)
        again = d.status == "retrying"
        status = d.status
    if again:
        logger.debug("webhook delivery {} retrying", delivery_id)
    return status


async def reset_stuck(tenant_id: uuid.UUID) -> int:
    """Worker crash: ``in_flight`` for > 60 s -> ``retrying`` now, without counting an attempt (same event id resent)."""
    cutoff = common.now() - timedelta(seconds=STUCK_SECONDS)
    async with tenant_session(tenant_id) as db:
        rows = list(
            (await db.execute(select(WebhookDelivery).where(WebhookDelivery.status == "in_flight", WebhookDelivery.updated_at < cutoff).with_for_update(skip_locked=True))).scalars()
        )
        for d in rows:
            d.status, d.next_attempt_at, d.updated_at = ("retrying" if d.attempt_count else "pending"), common.now(), common.now()
        return len(rows)


async def has_due(tenant_id: uuid.UUID) -> bool:
    async with tenant_session(tenant_id) as db:
        found = (
            await db.execute(
                select(WebhookDelivery.id).where(WebhookDelivery.status.in_(("pending", "retrying")), WebhookDelivery.next_attempt_at <= common.now()).limit(1)
            )
        ).first()
    return found is not None


__all__ = ["attempt", "deliver", "has_due", "jittered", "max_attempts", "plan_next", "pump", "reset_stuck"]
