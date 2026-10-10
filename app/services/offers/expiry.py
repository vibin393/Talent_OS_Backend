"""Offer expiry (FR-45, AD-9): the beat task body. Released / viewed offers whose ``expires_at`` has passed
move to ``expired`` (actor ``system``); rows are claimed with ``FOR UPDATE SKIP LOCKED`` so concurrent runs
never double-process, and a second run finds nothing (idempotent). ``OfferExpired`` makes the owner email go
out after commit."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import emit
from app.events.offers import OfferEmailRequested
from app.models.offers import Offer
from app.services.offers import status as status_machine
from app.services.offers.common import OPEN_FOR_RESPONSE, now


async def expire_due(ctx: RequestContext, db: AsyncSession) -> int:
    at = now()
    rows = list(
        (
            await db.execute(
                select(Offer)
                .where(
                    Offer.status.in_(OPEN_FOR_RESPONSE), Offer.expires_at.is_not(None), Offer.expires_at <= at
                )
                .order_by(Offer.expires_at)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for offer in rows:
        await status_machine.transition_offer(ctx, db, offer, "expired", "system", "expiry_date passed")
        emit(db, OfferEmailRequested(tenant_id=offer.tenant_id, offer_id=offer.id, kind="expired_owner"))
    return len(rows)
