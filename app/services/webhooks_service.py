# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Webhooks facade (Epic 68, AD-79/AD-80, FR-349..FR-353). Routers, the Developer portal (74), the marketplace (69) and
governance (79) call only this module. ``enqueue_delivery`` fills the Epic 66 stub with its frozen signature."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.webhooks import WebhookDelivery
from app.schemas.webhooks import EchoEndpointCreated, EchoEndpointOut, EchoReceiptOut, SubscriptionOut
from app.services.webhooks import common, echo, health, replay, retention, scheduler, sink, verification
from app.services.webhooks.log import export_csv, get_delivery, list_deliveries
from app.services.webhooks.replay import replay_delivery, replay_range
from app.services.webhooks.subscriptions import (
    allowed_event_types,
    apply_installation_change,
    create_subscription,
    delete_subscription,
    get_subscription,
    list_subscriptions,
    rotate_secret,
    set_enabled,
    update_subscription,
)
from app.services.webhooks.test_events import send_test_event


async def enqueue_delivery(
    ctx: RequestContext,
    db: AsyncSession,
    subscription_id: uuid.UUID,
    event_id: uuid.UUID,
    *,
    replay: bool = False,
) -> uuid.UUID:
    """The sink's per-pair insert (type + scope rules re-checked); ``replay=True`` always adds a new ``kind='replay'``
    row (bypassing the key table). Returns the delivery id (the existing one when the pair was already queued)."""
    from app.services import public_events_service
    from app.services.public_events.outbox import PublicEventRow

    sub = await common.load(ctx, db, subscription_id)
    if sub.status != "active":
        raise ConflictError("The webhook subscription is not active", code="subscription_not_active")
    ev = await public_events_service.get_event(RequestContext(tenant_id=ctx.tenant_id, actor_kind="system"), db, event_id)
    row = PublicEventRow(
        id=ev.id, seq=ev.sequence, tenant_id=ev.tenant_ref.id, event_type=ev.type, event_version=ev.version, visibility="public",
        subject_type=ev.subject.type, subject_id=ev.subject.id, occurred_at=ev.occurred_at, data=ev.data, actor_kind=None,
        app_installation_id=None,
    )  # fmt: skip
    inst = await common.installation(db, sub.installation_id) if sub.installation_id else None
    if row.event_type != sink.ERASED and not sink.matches(sub, row, inst):
        raise ValidationFailed("The event does not match this subscription", code="event_type_not_allowed")
    if replay:
        original = (
            await db.execute(select(WebhookDelivery).where(WebhookDelivery.subscription_id == sub.id, WebhookDelivery.event_id == event_id).order_by(WebhookDelivery.created_at).limit(1))
        ).scalar_one_or_none()
        if original is None:
            raise NotFound("No delivery of this event to replay")
        new = replay_mod.new_replay(sub, original, common.user_id_of(ctx))
        db.add(new)
        await db.flush()
        delivery_id = new.id
    else:
        made = await sink.insert_pair(db, sub, row)
        if made is None:
            existing = (
                await db.execute(select(WebhookDelivery.id).where(WebhookDelivery.subscription_id == sub.id, WebhookDelivery.event_id == event_id, WebhookDelivery.kind == "event").limit(1))
            ).scalar_one_or_none()
            if existing is None:  # pragma: no cover - key without row (purged log)
                raise NotFound("Delivery not found")
            return existing
        await db.flush()
        delivery_id = made
    assert ctx.tenant_id is not None
    common.kick_after_commit(db, ctx.tenant_id)
    return delivery_id


replay_mod = replay


async def verify_now(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID) -> SubscriptionOut:
    sub = await verification.verify_now(ctx, db, subscription_id)
    await db.refresh(sub)
    return await common.out(ctx, db, sub)


async def create_echo_endpoint(ctx: RequestContext, db: AsyncSession, label: str | None) -> EchoEndpointCreated:
    return await echo.create_endpoint(ctx, db, label)


async def list_echo_endpoints(ctx: RequestContext, db: AsyncSession) -> list[EchoEndpointOut]:
    return await echo.list_endpoints(ctx, db)


async def list_echo_receipts(ctx: RequestContext, db: AsyncSession, endpoint_id: uuid.UUID) -> list[EchoReceiptOut]:
    return await echo.list_receipts(ctx, db, endpoint_id)


async def receive_echo(token: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    return await echo.receive_public(token, headers, body)


# ------------------------------------------------------------------ task bodies
async def pump(tenant_id: uuid.UUID) -> list[uuid.UUID]:
    return await scheduler.pump(tenant_id)


async def deliver(tenant_id: uuid.UUID, delivery_id: uuid.UUID) -> str | None:
    return await scheduler.deliver(tenant_id, delivery_id)


async def has_due(tenant_id: uuid.UUID) -> bool:
    return await scheduler.has_due(tenant_id)


async def verify(tenant_id: uuid.UUID, subscription_id: uuid.UUID) -> int | None:
    return await verification.verify_task(tenant_id, subscription_id)


async def health_scan(tenant_id: uuid.UUID) -> int:
    return await health.scan(tenant_id)


async def purge(tenant_id: uuid.UUID) -> dict[str, int]:
    return await retention.purge(tenant_id)


async def replay_range_task(*args: object) -> int:
    return await replay.replay_range_task(*args)  # type: ignore[arg-type]


async def on_installation_changed(tenant_id: uuid.UUID, installation_id: uuid.UUID, change: str) -> int:
    from app.core.database import tenant_session

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        return await apply_installation_change(ctx, db, installation_id, change)


__all__ = [
    "allowed_event_types",
    "create_echo_endpoint",
    "create_subscription",
    "delete_subscription",
    "deliver",
    "enqueue_delivery",
    "export_csv",
    "get_delivery",
    "get_subscription",
    "has_due",
    "health_scan",
    "list_deliveries",
    "list_echo_endpoints",
    "list_echo_receipts",
    "list_subscriptions",
    "on_installation_changed",
    "pump",
    "purge",
    "receive_echo",
    "replay_delivery",
    "replay_range",
    "replay_range_task",
    "rotate_secret",
    "send_test_event",
    "set_enabled",
    "update_subscription",
    "verify",
    "verify_now",
]
