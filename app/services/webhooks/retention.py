# ruff: file-ignore[line-too-long]
"""Retention (Epic 68, spec A7; task ``webhooks.purge``, daily, ``maintenance``). The delivery log itself is purged by
the 66 partition job (``register_partitioned_table("webhook_deliveries", retention_days=30)``: whole months dropped,
older rows deleted). Here: keys older than 31 days, echo receipts older than 7 days, and soft-deleted Subscriptions
with no delivery left (hard delete; their vault secrets were removed at delete time)."""

import uuid
from datetime import timedelta

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.settings import get_settings
from app.models.webhooks import WebhookDelivery, WebhookDeliveryKey, WebhookEchoReceipt, WebhookSubscription
from app.services.webhooks import common

ECHO_DAYS = 7


async def purge_db(db: AsyncSession) -> dict[str, int]:
    now = common.now()
    days = get_settings().webhook_log_retention_days
    out: dict[str, int] = {}
    r = await db.execute(delete(WebhookDelivery).where(WebhookDelivery.created_at < now - timedelta(days=days)))
    out["deliveries"] = int(getattr(r, "rowcount", 0) or 0)
    r = await db.execute(delete(WebhookDeliveryKey).where(WebhookDeliveryKey.created_at < now - timedelta(days=days + 1)))
    out["keys"] = int(getattr(r, "rowcount", 0) or 0)
    r = await db.execute(delete(WebhookEchoReceipt).where(WebhookEchoReceipt.received_at < now - timedelta(days=ECHO_DAYS)))
    out["echo_receipts"] = int(getattr(r, "rowcount", 0) or 0)
    gone = list(
        (
            await db.execute(
                select(WebhookSubscription.id).where(
                    WebhookSubscription.deleted_at.is_not(None),
                    WebhookSubscription.deleted_at < now - timedelta(days=days),
                    ~exists().where(WebhookDelivery.subscription_id == WebhookSubscription.id),
                )
            )
        ).scalars()
    )
    if gone:
        await db.execute(delete(WebhookSubscription).where(WebhookSubscription.id.in_(gone)))
    out["subscriptions"] = len(gone)
    return out


async def purge(tenant_id: uuid.UUID) -> dict[str, int]:
    async with tenant_session(tenant_id) as db:
        return await purge_db(db)


__all__ = ["ECHO_DAYS", "purge", "purge_db"]
