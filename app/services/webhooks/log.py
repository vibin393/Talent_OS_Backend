# ruff: file-ignore[line-too-long]
"""The delivery log (Epic 68, story 68.5, FR-353): 30-day list / detail (attempts, response excerpts, the thin
request envelope) and the CSV export (ids, type, status, attempts, codes, latency -- never bodies). Visible only to the
owning Tenant (and, on the public surface, the owning App)."""

import csv
import io
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.public_api import decode_cursor, encode_cursor
from app.core.settings import get_settings
from app.models.webhooks import WebhookDelivery, WebhookSubscription
from app.schemas.public_events import PublicPage
from app.schemas.webhooks import Attempt, DeliveryDetailOut, DeliveryOut, DeliveryQuery
from app.services.webhooks import common


def _latency(d: WebhookDelivery) -> int | None:
    log = list(d.attempts or [])
    return log[-1].get("latency_ms") if log else None


def out(d: WebhookDelivery) -> DeliveryOut:
    return DeliveryOut(
        id=d.id, subscription_id=d.subscription_id, event_id=d.event_id, event_type=d.event_type, kind=d.kind,
        replay_of_id=d.replay_of_id, status=d.status, attempt_count=d.attempt_count, next_attempt_at=d.next_attempt_at,
        last_status_code=d.last_status_code, last_error=d.last_error, latency_ms=_latency(d), delivered_at=d.delivered_at,
        created_at=d.created_at,
    )  # fmt: skip


def window_start() -> datetime:
    return common.now() - timedelta(days=get_settings().webhook_log_retention_days)


async def load_delivery(ctx: RequestContext, db: AsyncSession, delivery_id: uuid.UUID, *, lock: bool = False) -> tuple[WebhookDelivery, WebhookSubscription]:
    stmt = select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
    if lock:
        stmt = stmt.with_for_update()
    d = (await db.execute(stmt)).scalar_one_or_none()
    if d is None:
        raise NotFound("Delivery not found")
    sub = await common.load(ctx, db, d.subscription_id, include_deleted=True)  # own-installation filter -> 404
    return d, sub


async def list_deliveries(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID, q: DeliveryQuery) -> PublicPage[DeliveryOut]:
    await common.load(ctx, db, subscription_id, include_deleted=True)
    stmt = select(WebhookDelivery).where(WebhookDelivery.subscription_id == subscription_id, WebhookDelivery.created_at >= max(window_start(), q.date_from or window_start()))
    if q.date_to:
        stmt = stmt.where(WebhookDelivery.created_at < q.date_to)
    if q.status:
        stmt = stmt.where(WebhookDelivery.status.in_(q.status))
    if q.event_type:
        stmt = stmt.where(WebhookDelivery.event_type == q.event_type)
    if q.cursor:
        (created,), last = decode_cursor(q.cursor)
        stmt = stmt.where(tuple_(WebhookDelivery.created_at, WebhookDelivery.id) < tuple_(datetime.fromisoformat(str(created)), last))
    rows = list((await db.execute(stmt.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc()).limit(q.limit + 1))).scalars())
    has_more = len(rows) > q.limit
    rows = rows[: q.limit]
    nxt = encode_cursor((rows[-1].created_at,), rows[-1].id) if has_more and rows else None
    return PublicPage[DeliveryOut](data=[out(r) for r in rows], next_cursor=nxt, has_more=has_more)


async def get_delivery(ctx: RequestContext, db: AsyncSession, delivery_id: uuid.UUID) -> DeliveryDetailOut:
    d, _ = await load_delivery(ctx, db, delivery_id)
    from app.services.webhooks.scheduler import _body

    body, _x = await _body(ctx, db, d)
    request: dict[str, Any] | None = None
    if body is not None:
        try:
            request = json.loads(body)
        except ValueError:
            request = None
    attempts = [Attempt.model_validate(a) for a in (d.attempts or [])]
    return DeliveryDetailOut(**out(d).model_dump(), attempts=attempts, request_body=request)


async def export_csv(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID | None, date_from: datetime | None, date_to: datetime | None) -> str:
    stmt = select(WebhookDelivery).where(WebhookDelivery.created_at >= max(window_start(), date_from or window_start()))
    if subscription_id is not None:
        await common.load(ctx, db, subscription_id, include_deleted=True)
        stmt = stmt.where(WebhookDelivery.subscription_id == subscription_id)
    if date_to is not None:
        stmt = stmt.where(WebhookDelivery.created_at < date_to)
    if date_from is not None and date_to is not None and date_to < date_from:
        raise ValidationFailed("'to' must be after 'from'")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["delivery_id", "subscription_id", "event_id", "event_type", "kind", "status", "attempts", "last_status_code", "last_error", "latency_ms", "created_at", "delivered_at"])
    for d in (await db.execute(stmt.order_by(WebhookDelivery.created_at).limit(100_000))).scalars():
        w.writerow([d.id, d.subscription_id, d.event_id, d.event_type, d.kind, d.status, d.attempt_count, d.last_status_code or "", d.last_error or "", _latency(d) or "", d.created_at.isoformat(), d.delivered_at.isoformat() if d.delivered_at else ""])
    return buf.getvalue()


__all__ = ["export_csv", "get_delivery", "list_deliveries", "load_delivery", "out", "window_start"]
