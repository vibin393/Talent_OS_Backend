# ruff: noqa: E501
"""Replay (Epic 68, story 68.5, FR-352/353): a new ``kind='replay'`` row with the SAME event id (``TalentOS-Replay:
true``); single or a ``failed|held`` range (<= 30 days, <= 10,000 rows, ascending ``event_seq``, task in chunks of
500). Only ``active`` Subscriptions (409 ``subscription_not_active``) and events inside the 30-day window (409
``replay_window_expired``)."""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.models.webhooks import WebhookDelivery, WebhookSubscription
from app.schemas.webhooks import DeliveryOut, ReplayRangeIn, ReplayRangeOut
from app.services.webhooks import common, log

MAX_RANGE_ROWS = 10_000
CHUNK = 500


def _active(sub: WebhookSubscription) -> None:
    if sub.status != "active" or sub.deleted_at is not None:
        raise ConflictError("The webhook subscription is not active", code="subscription_not_active")


def new_replay(sub: WebhookSubscription, original: WebhookDelivery, user_id: uuid.UUID | None) -> WebhookDelivery:
    at = common.now()
    return WebhookDelivery(
        id=uuid.uuid4(), tenant_id=sub.tenant_id, created_at=at, updated_at=at, subscription_id=sub.id,
        event_id=original.event_id, event_type=original.event_type, event_seq=original.event_seq,
        subject_type=original.subject_type, subject_id=original.subject_id, candidate_id=original.candidate_id,
        kind="replay", replay_of_id=original.id, body=original.body if original.kind == "test" else None, status="pending",
        next_attempt_at=at, created_by_user_id=user_id,
    )  # fmt: skip


async def replay_delivery(ctx: RequestContext, db: AsyncSession, delivery_id: uuid.UUID) -> DeliveryOut:
    d, sub = await log.load_delivery(ctx, db, delivery_id)
    _active(sub)
    if d.created_at < log.window_start():
        raise ConflictError("Events older than 30 days can't be replayed", code="replay_window_expired")
    row = new_replay(sub, d, common.user_id_of(ctx))
    db.add(row)
    audit.record("webhook.replayed", "webhook_subscription", sub.id, session=db, delivery_id=str(d.id), event_id=str(d.event_id), count=1)
    await db.flush()
    assert ctx.tenant_id is not None
    common.kick_after_commit(db, ctx.tenant_id)
    return log.out(row)


def _range_stmt(subscription_id: uuid.UUID, date_from: datetime, date_to: datetime, statuses: list[str]) -> Any:
    return select(WebhookDelivery).where(
        WebhookDelivery.subscription_id == subscription_id, WebhookDelivery.status.in_(statuses),
        WebhookDelivery.created_at >= date_from, WebhookDelivery.created_at < date_to,
    )  # fmt: skip


async def replay_range(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID, data: ReplayRangeIn) -> ReplayRangeOut:
    sub = await common.load(ctx, db, subscription_id)
    _active(sub)
    if data.date_to <= data.date_from or data.date_to - data.date_from > timedelta(days=31):
        raise ValidationFailed("The range must be at most 30 days", code="invalid_range")
    if data.date_from < log.window_start() - timedelta(minutes=5):
        raise ConflictError("Events older than 30 days can't be replayed", code="replay_window_expired")
    stmt = _range_stmt(sub.id, data.date_from, data.date_to, list(data.statuses))
    count = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    count = min(int(count), MAX_RANGE_ROWS)
    if data.preview or count == 0:
        return ReplayRangeOut(count=count, started=False)
    audit.record("webhook.replayed", "webhook_subscription", sub.id, session=db, count=count, statuses=list(data.statuses), range=True)
    from app.tasks import webhooks as tasks
    from app.tasks.runtime import enqueue

    assert ctx.tenant_id is not None
    args = (str(ctx.tenant_id), str(sub.id), data.date_from.isoformat(), data.date_to.isoformat(), list(data.statuses), str(common.user_id_of(ctx) or ""))
    import sqlalchemy

    sqlalchemy.event.listen(db.sync_session, "after_commit", lambda _s: enqueue(tasks.replay_range, *args), once=True)
    return ReplayRangeOut(count=count, started=True)


async def replay_range_task(tenant_id: uuid.UUID, subscription_id: uuid.UUID, date_from: datetime, date_to: datetime, statuses: list[str], user_id: uuid.UUID | None) -> int:
    from app.core.database import tenant_session

    created = 0
    last_seq: tuple[int, str] | None = None
    while created < MAX_RANGE_ROWS:
        async with tenant_session(tenant_id) as db:
            sub = (await db.execute(select(WebhookSubscription).where(WebhookSubscription.id == subscription_id))).scalar_one_or_none()
            if sub is None or sub.status != "active" or sub.deleted_at is not None:
                return created
            stmt = _range_stmt(subscription_id, date_from, date_to, statuses).where(WebhookDelivery.kind != "replay")
            rows = list((await db.execute(stmt.order_by(WebhookDelivery.event_seq.nulls_last(), WebhookDelivery.created_at, WebhookDelivery.id))).scalars())
            if last_seq is not None:
                ids = [str(r.id) for r in rows]
                rows = rows[ids.index(last_seq[1]) + 1 :] if last_seq[1] in ids else rows
            chunk = rows[: min(CHUNK, MAX_RANGE_ROWS - created)]
            if not chunk:
                return created
            for d in chunk:
                db.add(new_replay(sub, d, user_id))
            created += len(chunk)
            last_seq = (0, str(chunk[-1].id))
            common.kick_after_commit(db, tenant_id)
            if len(chunk) < CHUNK:
                return created
    return created


__all__ = ["new_replay", "replay_delivery", "replay_range", "replay_range_task"]
