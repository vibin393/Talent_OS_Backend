# ruff: file-ignore[line-too-long]
"""Webhooks erasure handler ``webhooks.deliveries`` (72), Epic 68 spec A7.

Deletes the erased subject's ``webhook_deliveries`` (``candidate_id`` or the candidate / worker subject) -- except the
``subject.erased`` deliveries themselves, which prove the D-11 notice went out -- and their ``webhook_delivery_keys``.
Bodies hold no personal data (thin envelopes); the ids are personal-data adjacent.
"""

from typing import Any

from sqlalchemy import and_, delete, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.webhooks import WebhookDelivery, WebhookDeliveryKey
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "subject.erased"


def _match(subject_id: Any) -> Any:
    return and_(
        WebhookDelivery.event_type != ERASED,
        or_(
            WebhookDelivery.candidate_id == subject_id,
            and_(WebhookDelivery.subject_type.in_(("candidate", "worker")), WebhookDelivery.subject_id == subject_id),
        ),
    )


class WebhooksHandler:
    name = "webhooks.deliveries"
    order = 72
    tables = ("webhook_deliveries", "webhook_delivery_keys")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        sid = ctx.subject_id or ctx.candidate_id
        pairs = list((await db.execute(select(WebhookDelivery.subscription_id, WebhookDelivery.event_id).where(_match(sid)))).all())
        keys = 0
        if pairs:
            r = await db.execute(
                delete(WebhookDeliveryKey).where(tuple_(WebhookDeliveryKey.subscription_id, WebhookDeliveryKey.event_id).in_([tuple(p) for p in pairs]))
            )
            keys = int(getattr(r, "rowcount", 0) or 0)
        r = await db.execute(delete(WebhookDelivery).where(_match(sid)))
        await db.flush()
        return {"deliveries": int(getattr(r, "rowcount", 0) or 0), "keys": keys}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:  # ruff: ignore[unused-method-argument]
        return None  # delivery metadata only (ids, types, statuses): nothing personal to export

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        sid = ctx.subject_id or ctx.candidate_id
        left = (await db.execute(select(WebhookDelivery.id).where(_match(sid)).limit(1))).first()
        return ["webhook delivery remains"] if left else []


register_erasure_handler(WebhooksHandler())
